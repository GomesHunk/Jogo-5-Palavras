import eventlet
eventlet.monkey_patch()

from flask import Flask, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import time
import os
import random
import string

# --- Constantes do Jogo ---
NUM_WORDS_PER_PLAYER = 5 # O jogo é "Jogo das 5 Palavras"
MAX_PLAYERS_PER_ROOM = 2
MAX_WORD_LENGTH = 25
MIN_WORD_LENGTH = 2
ROOM_CODE_LENGTH = 6
ROOM_CLEANUP_INTERVAL_SECONDS = 600 
ROOM_EXPIRY_SECONDS = 3600 
# --- Fim Constantes ---

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'desenvolvimento_secret_key_placeholder')
CORS(app, resources={r"/*": {"origins": "*"}}) # Permite todas as origens para CORS
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
        self.last_action_result = None # Para toasts no front-end

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
        if sid in self.game_data_for_players: # Limpa também os dados de jogo específicos
            del self.game_data_for_players[sid]
        # players_online é gerenciado fora da sala
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
        for sid_player in self.players: # Itera sobre as chaves (sids)
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
    if active_player_sid not in room_obj.players: # Jogador da vez não existe mais na sala
        print(f"ERRO broadcast: Jogador da vez {active_player_sid} não encontrado na sala {room_obj.room_id}")
        # Aqui poderia ter lógica para selecionar novo jogador ou terminar o jogo
        return
        
    active_player_name = room_obj.players[active_player_sid]["name"]
    active_player_challenge = room_obj.game_data_for_players.get(active_player_sid)

    if not active_player_challenge:
        print(f"ERRO broadcast: Dados de desafio não encontrados para {active_player_name} (SID: {active_player_sid})")
        return

    keyword = active_player_challenge["words_to_guess_original"][0]
    initials = [w[0].upper() for w in active_player_challenge["words_to_guess_original"][1:]]
    all_target_words = active_player_challenge["words_to_guess_original"]
    completed_mask = active_player_challenge["completed_words_mask"]
    current_word_idx = active_player_challenge["current_word_idx_guessing"]
    
    hint_for_active = "JOGO COMPLETO"
    if current_word_idx < NUM_WORDS_PER_PLAYER:
        word_lc = active_player_challenge["words_to_guess_lowercase"][current_word_idx]
        revealed = active_player_challenge["revealed_letters_count"][current_word_idx]
        hint_for_active = generate_hint(word_lc, revealed)
    else:
        current_word_idx = NUM_WORDS_PER_PLAYER 

    base_payload = {
        "viewing_for_player_name": active_player_name,
        "keyword_to_display": keyword,
        "initials_to_display": initials,
        "all_target_words_original_for_progress": all_target_words,
        "completed_mask_for_progress": completed_mask,
        "active_word_idx_being_guessed": current_word_idx,
        "current_hint": hint_for_active,
        "actual_current_turn_player_name": active_player_name, 
        "last_action_result": room_obj.last_action_result
    }
    room_obj.last_action_result = None 

    for sid_in_room in list(room_obj.players.keys()): # list() para cópia segura se players mudar
        payload_for_client = base_payload.copy()
        payload_for_client["is_your_turn"] = (sid_in_room == active_player_sid)
        socketio.emit("update_game_display", payload_for_client, room=sid_in_room)

@app.route("/")
def index():
    return "Servidor Jogo das 5 Palavras Online!"

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
            room.remove_player(sid)
            socketio.emit("player_left", {"player_name": disconnected_player_name, "players_online": get_room_players_info(room)}, room=room_id)
            if room.game_state == "playing" and len(room.players) == 1:
                remaining_player_sid = list(room.players.keys())[0]
                remaining_player_name = room.players[remaining_player_sid]["name"]
                room.game_state = "finished"
                socketio.emit("game_finished", {
                    "winner_name": remaining_player_name,
                    "message": f"{remaining_player_name} venceu! {disconnected_player_name} desconectou.",
                    "was_disconnection": True
                }, room=remaining_player_sid)
            if not room.players: print(f"Sala {room_id} ficou vazia.")

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
    if not player_info: emit("error", {"message": "Jogador não encontrado."}); return
    room = rooms.get(player_info["room"])
    if not room: emit("error", {"message": "Sala não encontrada."}); return

    words_list = data.get("words", [])
    if len(words_list) != NUM_WORDS_PER_PLAYER:
        emit("error", {"message": f"Envie {NUM_WORDS_PER_PLAYER} palavras."}); return
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
    p1_sid, p2_sid = sids[0], sids[1]

    room_obj.game_data_for_players = {
        p1_sid: {"words_to_guess_original": room_obj.words_chosen_data[p2_sid]['original'],
                 "words_to_guess_lowercase": room_obj.words_chosen_data[p2_sid]['lowercase'],
                 "current_word_idx_guessing": 1, # Advinha a partir da segunda palavra
                 "revealed_letters_count": [len(room_obj.words_chosen_data[p2_sid]['original'][0])] + [1] * (NUM_WORDS_PER_PLAYER -1), # Primeira palavra (keyword) totalmente revelada para contagem
                 "completed_words_mask": [True] + [False] * (NUM_WORDS_PER_PLAYER - 1)}, # Keyword é "completa"
        p2_sid: {"words_to_guess_original": room_obj.words_chosen_data[p1_sid]['original'],
                 "words_to_guess_lowercase": room_obj.words_chosen_data[p1_sid]['lowercase'],
                 "current_word_idx_guessing": 1,
                 "revealed_letters_count": [len(room_obj.words_chosen_data[p1_sid]['original'][0])] + [1] * (NUM_WORDS_PER_PLAYER -1),
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
        emit("error", {"message": "Não é sua vez ou jogo não ativo."}); return
    
    guess_orig = data.get("guess", "").strip()
    guess_lc = guess_orig.lower()
    if not guess_lc: emit("error", {"message": "Palpite vazio."}); return

    active_player_challenge = room.game_data_for_players[sid]
    idx_guessing = active_player_challenge["current_word_idx_guessing"]

    if idx_guessing >= NUM_WORDS_PER_PLAYER: # Já completou tudo
        broadcast_game_state(room); return # Apenas reenvia o estado atual

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
                "message": f"🎉 {player_info['name']} venceu!",
                "final_words_sequence": active_player_challenge["words_to_guess_original"], # Sequência que ele adivinhou
                "was_disconnection": False
            }, room=room.room_id)
            return 
    else:
        active_player_challenge["revealed_letters_count"][idx_guessing] += 1
        room.current_turn_sid = room.get_opponent_sid(sid)
        room.last_action_result = {"type": "incorrect_guess", "by_player_name": player_info["name"], "attempted_word": guess_orig}
    
    broadcast_game_state(room)

@socketio.on("chat_message")
def handle_chat_message(data):
    sid = request.sid
    player_info = players_online.get(sid)
    if not player_info: return
    room = rooms.get(player_info["room"])
    if not room: return
    message = data.get("message", "").strip()
    if message and len(message) <= 150:
        socketio.emit("new_chat_message", {"player_name": player_info["name"], "message": message, "timestamp": time.strftime("%H:%M")}, room=room.room_id)

@socketio.on("request_new_game")
def handle_request_new_game(data):
    sid = request.sid
    player_info = players_online.get(sid)
    if not player_info: return
    room = rooms.get(player_info["room"])
    if not room or room.game_state != "finished":
        emit("error", {"message": "Só pode iniciar novo jogo após o término."}); return
    
    room.reset_game_state_for_new_round()
    socketio.emit("game_reset_initiated", {"message": f"{player_info['name']} iniciou novo jogo!"}, room=room.room_id)
    if len(room.players) == MAX_PLAYERS_PER_ROOM:
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
        leave_room(room_id) # SocketIO specific leave

        opponent_sid = room.get_opponent_sid(None)
        if opponent_sid and opponent_sid in room.players:
            opponent_name = room.players[opponent_sid]["name"]
            socketio.emit("opponent_left_game", {"leaver_name": leaving_player_name, "message": f"{leaving_player_name} saiu."}, room=opponent_sid)
            if room.game_state == "playing" or room.game_state == "choosing_words":
                room.game_state = "finished"
                socketio.emit("game_finished", {
                    "winner_name": opponent_name, "message": f"{opponent_name} venceu! {leaving_player_name} saiu.",
                    "was_disconnection": True # Trata como W.O. para oponente
                }, room=opponent_sid)
            socketio.emit("players_update", {"players_online": get_room_players_info(room)}, room=opponent_sid)
        
        if not room.players: print(f"Sala {room_id} vazia após saída voluntária.")
        else: room.game_state = "waiting" # Ou estado apropriado

def cleanup_inactive_rooms():
    current_time = time.time()
    for room_id in list(rooms.keys()):
        room = rooms[room_id]
        can_delete = False
        if not room.players and (current_time - room.created_at > 300): # Vazia por 5 mins
            can_delete = True
        if current_time - room.created_at > ROOM_EXPIRY_SECONDS: # Muito antiga
             can_delete = True
        if room.game_state == "playing" and room.players: # Não deletar se estiver em jogo com jogadores
            can_delete = False
            
        if can_delete:
            print(f"Limpando sala inativa/expirada: {room_id}")
            del rooms[room_id]

def scheduled_cleanup_task():
    with app.app_context(): 
        while True:
            socketio.sleep(ROOM_CLEANUP_INTERVAL_SECONDS) 
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Limpeza de salas...")
            cleanup_inactive_rooms()
            print(f"Salas ativas: {len(rooms)}")

if __name__ == "__main__":
    print("Iniciando tarefa de limpeza...")
    eventlet.spawn(scheduled_cleanup_task)
    port = int(os.environ.get("PORT", 5000))
    host = "0.0.0.0"
    print(f"Servidor iniciando em {host}:{port}")
    socketio.run(app, host=host, port=port, debug=False, use_reloader=False)
