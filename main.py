import eventlet
eventlet.monkey_patch()

from flask import Flask, request, render_template # render_template adicionado
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import time
import os
import random
import string

# --- Constantes do Jogo ---
NUM_WORDS_PER_PLAYER = 5 
MAX_PLAYERS_PER_ROOM = 2
MAX_WORD_LENGTH = 25
MIN_WORD_LENGTH = 2
ROOM_CODE_LENGTH = 6
ROOM_CLEANUP_INTERVAL_SECONDS = 600 
ROOM_EXPIRY_SECONDS = 3600 
# --- Fim Constantes ---

app = Flask(__name__, template_folder='templates') # Especifica a pasta de templates
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'uma_chave_secreta_muito_forte_para_prod') # Use variável de ambiente em produção
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

rooms = {} 
players_online = {} 

def generate_room_code(length=ROOM_CODE_LENGTH):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

class GameRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = {} 
        self.words_chosen_data = {} 
        self.current_turn_sid = None
        self.game_state = "waiting" 
        self.game_data_for_players = {} 
        self.created_at = time.time()
        self.last_action_result = None 

    def add_player(self, sid, player_name):
        if len(self.players) < MAX_PLAYERS_PER_ROOM and sid not in self.players:
            self.players[sid] = {"name": player_name, "words_chosen": False}
            players_online[sid] = {"name": player_name, "room": self.room_id}
            return True
        return False

    def remove_player(self, sid):
        player_name_removed = None
        if sid in self.players:
            player_name_removed = self.players[sid]["name"]
            del self.players[sid]
        if sid in self.words_chosen_data:
            del self.words_chosen_data[sid]
        if sid in self.game_data_for_players:
            del self.game_data_for_players[sid]
        return player_name_removed

    def all_players_chosen_words(self):
        if len(self.players) != MAX_PLAYERS_PER_ROOM: return False
        return all(p_data["words_chosen"] for p_data in self.players.values())

    def get_opponent_sid(self, player_sid):
        for sid_in_room in self.players.keys():
            if sid_in_room != player_sid:
                return sid_in_room
        return None

    def reset_game_state_for_new_round(self):
        self.words_chosen_data = {}
        for sid_player in self.players:
            self.players[sid_player]["words_chosen"] = False
        self.current_turn_sid = None
        self.game_state = "choosing_words" if len(self.players) == MAX_PLAYERS_PER_ROOM else "waiting"
        self.game_data_for_players = {}
        self.last_action_result = None

def generate_hint(word_lc, num_revealed):
    if not word_lc: return "_" 
    if num_revealed >= len(word_lc): return word_lc.upper()
    return word_lc[:num_revealed].upper() + "_" * (len(word_lc) - num_revealed)

def get_room_players_info(room_obj):
    if not room_obj: return []
    return [{
        "name": p_data["name"],
        "words_chosen": p_data["words_chosen"],
        "is_current_turn": room_obj.current_turn_sid is not None and room_obj.current_turn_sid == sid
    } for sid, p_data in room_obj.players.items()]

def broadcast_game_state(room_obj):
    if not room_obj or room_obj.game_state != "playing" or not room_obj.current_turn_sid:
        return

    active_player_sid = room_obj.current_turn_sid
    if active_player_sid not in room_obj.players: 
        print(f"ERRO broadcast: Jogador da vez {active_player_sid} não encontrado na sala {room_obj.room_id}")
        # Potencialmente resetar o turno ou finalizar o jogo se o jogador da vez sumiu
        # Por agora, apenas retorna para evitar crash.
        return
        
    active_player_name = room_obj.players[active_player_sid]["name"]
    active_player_challenge = room_obj.game_data_for_players.get(active_player_sid)

    if not active_player_challenge:
        print(f"ERRO broadcast: Dados de desafio não encontrados para {active_player_name} (SID: {active_player_sid}) na sala {room_obj.room_id}")
        return

    keyword = active_player_challenge["words_to_guess_original"][0]
    # Iniciais são das palavras DEPOIS da keyword
    initials = [w[0].upper() for w in active_player_challenge["words_to_guess_original"][1:]] if len(active_player_challenge["words_to_guess_original"]) > 1 else []
    
    all_target_words = active_player_challenge["words_to_guess_original"]
    completed_mask = active_player_challenge["completed_words_mask"]
    current_word_idx_guessing = active_player_challenge["current_word_idx_guessing"]
    
    hint_for_active = "SEQUÊNCIA COMPLETA!" # Se todas as palavras (após a keyword) foram adivinhadas
    # O índice current_word_idx_guessing refere-se à lista de 5 palavras. 
    # Se ele é 0, é a keyword. Se é 1, é a primeira palavra a adivinhar ativamente.
    if current_word_idx_guessing < NUM_WORDS_PER_PLAYER: # Certifica que o índice é válido
        word_lc_for_hint = active_player_challenge["words_to_guess_lowercase"][current_word_idx_guessing]
        revealed_count_for_hint = active_player_challenge["revealed_letters_count"][current_word_idx_guessing]
        hint_for_active = generate_hint(word_lc_for_hint, revealed_count_for_hint)
    else: # Caso onde current_word_idx_guessing >= NUM_WORDS_PER_PLAYER (todas adivinhadas)
        current_word_idx_guessing = NUM_WORDS_PER_PLAYER # Para consistência no front-end

    base_payload = {
        "viewing_for_player_name": active_player_name,
        "keyword_to_display": keyword,
        "initials_to_display": initials,
        "all_target_words_original_for_progress": all_target_words,
        "completed_mask_for_progress": completed_mask,
        "active_word_idx_being_guessed": current_word_idx_guessing, # Qual palavra (0-4) a dica se refere
        "current_hint": hint_for_active,
        "actual_current_turn_player_name": active_player_name, 
        "last_action_result": room_obj.last_action_result
    }
    room_obj.last_action_result = None 

    for sid_in_room in list(room_obj.players.keys()):
        payload_for_client = base_payload.copy()
        payload_for_client["is_your_turn"] = (sid_in_room == active_player_sid)
        socketio.emit("update_game_display", payload_for_client, room=sid_in_room)

@app.route("/")
def index():
    return render_template("index.html") # Servindo o frontend

@socketio.on("connect")
def on_connect():
    print(f"Cliente conectado: {request.sid}")
    emit("connected", {"message": "Conectado ao servidor."})

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    print(f"Cliente desconectado: {request.sid}")
    player_info = players_online.pop(sid, None)
    if player_info:
        room_id = player_info['room']
        disconnected_player_name = player_info['name']
        if room_id in rooms:
            room = rooms[room_id]
            room.remove_player(sid) # Remove dos dados da sala
            socketio.emit("player_left", {"player_name": disconnected_player_name, "players_online": get_room_players_info(room)}, room=room_id)
            if room.game_state == "playing" and len(room.players) == 1:
                remaining_player_sid = list(room.players.keys())[0] # Pega o único jogador que sobrou
                if remaining_player_sid in room.players: # Checagem extra
                    remaining_player_name = room.players[remaining_player_sid]["name"]
                    room.game_state = "finished"
                    socketio.emit("game_finished", {
                        "winner_name": remaining_player_name,
                        "message": f"{remaining_player_name} venceu! {disconnected_player_name} desconectou.",
                        "final_words_sequence": room.game_data_for_players.get(remaining_player_sid, {}).get("words_to_guess_original", []), # Sequência que ele estava adivinhando
                        "was_disconnection": True
                    }, room=remaining_player_sid) # Envia só para o jogador que sobrou
            if not room.players: 
                print(f"Sala {room_id} ficou vazia após desconexão.")
                # A limpeza periódica cuidará de remover a sala do dict `rooms`

@socketio.on("join_game")
def handle_join_game(data):
    player_name = data.get("player_name", "").strip()
    room_id_req = data.get("room_id", "").strip().upper()
    sid = request.sid

    if not player_name or len(player_name) < MIN_WORD_LENGTH or len(player_name) > 20:
        emit("error", {"message": "Nome inválido (2-20 caracteres)."}); return

    target_room = None
    is_new_room = False
    if not room_id_req:
        room_id_req = generate_room_code()
        target_room = GameRoom(room_id_req)
        rooms[room_id_req] = target_room
        is_new_room = True
    elif room_id_req in rooms:
        target_room = rooms[room_id_req]
    else:
        emit("error", {"message": "Sala não encontrada."}); return

    if len(target_room.players) >= MAX_PLAYERS_PER_ROOM and sid not in target_room.players:
        emit("error", {"message": "Sala cheia."}); return

    join_room(room_id_req)
    target_room.add_player(sid, player_name)
    emit("joined_room", {"room_id": room_id_req, "player_name": player_name, "is_creator": is_new_room})
    socketio.emit("players_update", {"players_online": get_room_players_info(target_room)}, room=room_id_req)

    if len(target_room.players) == MAX_PLAYERS_PER_ROOM and target_room.game_state != "playing":
        target_room.game_state = "choosing_words"
        socketio.emit("start_choosing_words", {"message": "Ambos conectados! Escolham suas palavras."}, room=room_id_req)

@socketio.on("submit_words")
def handle_submit_words(data):
    sid = request.sid
    player_info = players_online.get(sid)
    if not player_info: emit("error", {"message": "Jogador não autenticado."}); return # Melhor mensagem
    room = rooms.get(player_info["room"])
    if not room: emit("error", {"message": "Sala não encontrada para o jogador."}); return

    words_list = data.get("words", [])
    if len(words_list) != NUM_WORDS_PER_PLAYER:
        emit("error", {"message": f"Por favor, envie exatamente {NUM_WORDS_PER_PLAYER} palavras."}); return
    for i, w_str in enumerate(words_list):
        w = w_str.strip()
        if not w or len(w) < MIN_WORD_LENGTH or len(w) > MAX_WORD_LENGTH:
            emit("error", {"message": f"Palavra {i+1} inválida ({MIN_WORD_LENGTH}-{MAX_WORD_LENGTH} caracteres)."}); return
    
    room.words_chosen_data[sid] = {'original': [w.strip() for w in words_list], 'lowercase': [w.strip().lower() for w in words_list]}
    room.players[sid]["words_chosen"] = True
    socketio.emit("words_submitted_update", {"player_name": player_info["name"], "players_online": get_room_players_info(room)}, room=room.room_id)
    if room.all_players_chosen_words():
        initialize_game_round(room)

def initialize_game_round(room_obj):
    room_obj.game_state = "playing"
    sids = list(room_obj.players.keys())
    if len(sids) < MAX_PLAYERS_PER_ROOM: 
        print(f"ERRO initialize_game_round: Menos de {MAX_PLAYERS_PER_ROOM} jogadores na sala {room_obj.room_id}")
        return # Não pode iniciar o jogo
    
    p1_sid, p2_sid = sids[0], sids[1]

    p1_words_orig = room_obj.words_chosen_data[p1_sid]['original']
    p1_words_lc = room_obj.words_chosen_data[p1_sid]['lowercase']
    p2_words_orig = room_obj.words_chosen_data[p2_sid]['original']
    p2_words_lc = room_obj.words_chosen_data[p2_sid]['lowercase']

    room_obj.game_data_for_players = {
        p1_sid: {"words_to_guess_original": p2_words_orig, # Jogador 1 adivinha palavras do Jogador 2
                 "words_to_guess_lowercase": p2_words_lc,
                 "current_word_idx_guessing": 1, # Começa adivinhando a palavra de índice 1 (a segunda)
                 "revealed_letters_count": [len(p2_words_orig[0])] + [1] * (NUM_WORDS_PER_PLAYER - 1), # Contagem para a keyword e 1 para as outras
                 "completed_words_mask": [True] + [False] * (NUM_WORDS_PER_PLAYER - 1)}, # Keyword (índice 0) é "completa" por ser dada
        p2_sid: {"words_to_guess_original": p1_words_orig, # Jogador 2 adivinha palavras do Jogador 1
                 "words_to_guess_lowercase": p1_words_lc,
                 "current_word_idx_guessing": 1, 
                 "revealed_letters_count": [len(p1_words_orig[0])] + [1] * (NUM_WORDS_PER_PLAYER - 1),
                 "completed_words_mask": [True] + [False] * (NUM_WORDS_PER_PLAYER - 1)}
    }
    room_obj.current_turn_sid = random.choice(sids)
    room_obj.last_action_result = {"type": "game_start"}
    broadcast_game_state(room_obj)

@socketio.on("make_guess")
def handle_make_guess(data):
    sid = request.sid
    player_info = players_online.get(sid)
    if not player_info: return
    room = rooms.get(player_info["room"])
    if not room: return

    if room.game_state != "playing" or room.current_turn_sid != sid:
        emit("error", {"message": "Não é sua vez ou o jogo não está ativo."}); return
    
    guess_orig_case = data.get("guess", "").strip()
    guess_lc = guess_orig_case.lower()
    if not guess_lc: 
        # Não envia erro para o cliente, apenas ignora ou loga no servidor, pois o front deve validar
        print(f"Jogador {player_info['name']} enviou palpite vazio.")
        return

    active_player_challenge = room.game_data_for_players[sid]
    idx_guessing = active_player_challenge["current_word_idx_guessing"]

    if idx_guessing >= NUM_WORDS_PER_PLAYER: 
        broadcast_game_state(room); return 

    target_lc = active_player_challenge["words_to_guess_lowercase"][idx_guessing]
    target_orig = active_player_challenge["words_to_guess_original"][idx_guessing]

    if guess_lc == target_lc:
        active_player_challenge["completed_words_mask"][idx_guessing] = True
        active_player_challenge["current_word_idx_guessing"] += 1
        room.last_action_result = {"type": "correct_guess", "by_player_name": player_info["name"], "word": target_orig}
        
        if active_player_challenge["current_word_idx_guessing"] >= NUM_WORDS_PER_PLAYER:
            room.game_state = "finished"
            socketio.emit("game_finished", {
                "winner_name": player_info["name"],
                "message": f"🎉 {player_info['name']} venceu o jogo!",
                "final_words_sequence": active_player_challenge["words_to_guess_original"], 
                "was_disconnection": False
            }, room=room.room_id) # Envia para todos na sala
            return 
    else: # Errou
        # Aumenta o número de letras reveladas para a palavra atual que ele está tentando adivinhar
        if idx_guessing < NUM_WORDS_PER_PLAYER : # Checagem de segurança de índice
             active_player_challenge["revealed_letters_count"][idx_guessing] = min(
                len(target_lc), # Não pode revelar mais letras do que a palavra tem
                active_player_challenge["revealed_letters_count"][idx_guessing] + 1
            )
        room.current_turn_sid = room.get_opponent_sid(sid) # Passa a vez
        room.last_action_result = {"type": "incorrect_guess", "by_player_name": player_info["name"], "attempted_word": guess_orig_case}
    
    broadcast_game_state(room)

@socketio.on("chat_message")
def handle_chat_message(data):
    sid = request.sid
    player_info = players_online.get(sid)
    if not player_info: return
    room = rooms.get(player_info["room"])
    if not room: return
    message = data.get("message", "").strip()
    if message and 0 < len(message) <= 150: # Validação de tamanho
        socketio.emit("new_chat_message", {
            "player_name": player_info["name"], 
            "message": message, # Idealmente, sanitizar no front-end antes de exibir
            "timestamp": time.strftime("%H:%M")
        }, room=room.room_id)

@socketio.on("request_new_game")
def handle_request_new_game(data):
    sid = request.sid
    player_info = players_online.get(sid)
    if not player_info: return
    room = rooms.get(player_info["room"])
    if not room or room.game_state != "finished":
        emit("error", {"message": "Só é possível iniciar um novo jogo após o término do atual."}); return
    
    # Aqui, idealmente, o outro jogador também precisaria concordar ou ser notificado
    # Por simplicidade, um jogador pode resetar para a tela de escolha de palavras
    room.reset_game_state_for_new_round()
    socketio.emit("game_reset_initiated", {"message": f"{player_info['name']} iniciou uma nova partida!"}, room=room.room_id)
    if len(room.players) == MAX_PLAYERS_PER_ROOM: # Se ambos ainda estão na sala
        room.game_state = "choosing_words"
        socketio.emit("start_choosing_words", {"message": "Escolham suas palavras para a nova rodada."}, room=room.room_id)

@socketio.on("player_initiated_leave")
def handle_player_initiated_leave():
    sid = request.sid    
    player_info = players_online.pop(sid, None) 
    if not player_info: return

    room_id = player_info['room']
    leaving_player_name = player_info['name']
    print(f"Jogador {leaving_player_name} (SID:{sid}) saindo voluntariamente da sala {room_id}")

    if room_id in rooms:
        room = rooms[room_id]
        room.remove_player(sid) 
        leave_room(room_id) 

        opponent_sid = room.get_opponent_sid(None)
        if opponent_sid and opponent_sid in room.players: # Se oponente ainda está na sala
            opponent_name = room.players[opponent_sid]["name"]
            socketio.emit("opponent_left_game", {"leaver_name": leaving_player_name, "message": f"{leaving_player_name} saiu da partida."}, room=opponent_sid)
            if room.game_state == "playing" or room.game_state == "choosing_words":
                room.game_state = "finished"
                socketio.emit("game_finished", {
                    "winner_name": opponent_name, 
                    "message": f"{opponent_name} venceu pois {leaving_player_name} saiu.",
                    "final_words_sequence": room.game_data_for_players.get(opponent_sid, {}).get("words_to_guess_original", []),
                    "was_disconnection": True 
                }, room=opponent_sid)
            socketio.emit("players_update", {"players_online": get_room_players_info(room)}, room=opponent_sid)
        
        if not room.players: 
            print(f"Sala {room_id} ficou vazia após saída voluntária de {leaving_player_name}.")
        else: # Se oponente saiu, mas o jogo não estava ativo (ex: tela de espera)
            room.game_state = "waiting" 
            
def cleanup_inactive_rooms():
    current_time = time.time()
    for room_id in list(rooms.keys()): 
        room = rooms[room_id]
        can_delete = False
        # Vazia por mais de 5 minutos (300s)
        if not room.players and (current_time - room.created_at > 300): 
            can_delete = True
        # Ou muito antiga (1 hora)
        if current_time - room.created_at > ROOM_EXPIRY_SECONDS: 
             can_delete = True
        
        # Não deletar salas que estão em jogo ativo, mesmo se antigas (improvável com o timeout acima)
        if room.game_state == "playing" and len(room.players) > 0 :
            can_delete = False 
            
        if can_delete:
            print(f"Limpando sala inativa/expirada: {room_id}")
            del rooms[room_id]

def scheduled_cleanup_task():
    # Adicionado 'with app.app_context()' se a limpeza precisar de acesso ao contexto da app Flask
    # Para este código, não parece ser estritamente necessário, mas é uma boa prática.
    with app.app_context(): 
        while True:
            socketio.sleep(ROOM_CLEANUP_INTERVAL_SECONDS) 
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Executando tarefa de limpeza de salas...")
            cleanup_inactive_rooms()
            print(f"Salas ativas após limpeza: {len(rooms)}")

if __name__ == "__main__":
    print("Iniciando tarefa de limpeza de salas em background...")
    eventlet.spawn(scheduled_cleanup_task)
    
    port = int(os.environ.get("PORT", 5000))
    # Para Render ou outros PaaS, host="0.0.0.0" é usual.
    host = os.environ.get("HOST", "0.0.0.0") 
    print(f"Servidor Socket.IO iniciando em {host}:{port}")
    # Em produção, debug=False. use_reloader=False é importante para eventlet.
    socketio.run(app, host=host, port=port, debug=True, use_reloader=False)
