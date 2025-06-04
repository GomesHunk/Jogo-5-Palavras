import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, render_template_string, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import time
import os
import random
import string

# --- Constantes do Jogo ---
NUM_WORDS_PER_PLAYER = 5
MAX_PLAYERS_PER_ROOM = 2
MAX_WORD_LENGTH = 25 # Reduzido do front-end (era 50 no back, 25 no front)
ROOM_CODE_LENGTH = 6
ROOM_CLEANUP_INTERVAL_SECONDS = 600 # 10 minutos
ROOM_EXPIRY_SECONDS = 3600 # 1 hora
# --- Fim Constantes ---

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'uma_chave_secreta_bem_forte!') # Melhor usar variável de ambiente
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

rooms = {}
players_online = {} # {sid: {"name": player_name, "room": room_id}}

def generate_room_code(length=ROOM_CODE_LENGTH):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

class GameRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = {} # {sid: {"name": player_name, "words_chosen": False}}
        self.words_chosen_data = {} # {sid: {'original': [], 'lowercase': []}}
        self.current_turn_sid = None
        self.game_state = "waiting" # waiting, choosing_words, playing, finished
        self.game_data = None # Detalhes específicos do jogo em andamento
        self.created_at = time.time()

    def add_player(self, sid, player_name):
        if len(self.players) < MAX_PLAYERS_PER_ROOM and sid not in self.players:
            self.players[sid] = {"name": player_name, "words_chosen": False}
            players_online[sid] = {"name": player_name, "room": self.room_id}
            return True
        return False

    def remove_player(self, sid):
        if sid in self.players:
            del self.players[sid]
        if sid in self.words_chosen_data:
            del self.words_chosen_data[sid]
        if sid in players_online:
            del players_online[sid]

    def all_players_chosen_words(self):
        if len(self.players) != MAX_PLAYERS_PER_ROOM:
            return False
        return all(player_data["words_chosen"] for player_data in self.players.values())

    def get_opponent_sid(self, player_sid):
        for sid_in_room in self.players.keys():
            if sid_in_room != player_sid:
                return sid_in_room
        return None

    def reset_game_state(self):
        self.words_chosen_data = {}
        for sid in self.players:
            self.players[sid]["words_chosen"] = False
        self.current_turn_sid = None
        self.game_state = "choosing_words" # Ou "waiting" se < MAX_PLAYERS
        self.game_data = None

@app.route("/")
def index():
    return render_template("index.html") # Assume que seu HTML está em templates/index.html

@socketio.on("connect")
def on_connect():
    emit("connected", {"message": "Conectado ao servidor."})
    print(f"Cliente conectado: {request.sid}")

@socketio.on("disconnect")
def on_disconnect():
    print(f"Cliente desconectado: {request.sid}")
    sid = request.sid
    if sid in players_online:
        room_id = players_online[sid]['room']
        player_name = players_online[sid]['name']
        
        if room_id in rooms:
            room = rooms[room_id]
            room.remove_player(sid)
            
            socketio.emit("player_left", {
                "player_name": player_name,
                "players_online": get_room_players_info(room)
            }, room=room_id)

            if room.game_state == "playing" and len(room.players) < MAX_PLAYERS_PER_ROOM :
                opponent_sid = room.get_opponent_sid(None) # Pega o jogador restante
                if opponent_sid:
                    socketio.emit("opponent_disconnected", {
                        "message": f"{player_name} desconectou. Você venceu por W.O.!",
                        "winner_name": room.players[opponent_sid]["name"]
                    }, room=opponent_sid)
                room.game_state = "finished"

            if not room.players: # Se a sala ficou vazia
                print(f"Sala {room_id} vazia, agendando para remoção se não utilizada.")
                # A limpeza periódica cuidará disso
    # Limpeza já está no dict global players_online
    if sid in players_online:
        del players_online[sid]


@socketio.on("join_game") # Renomeado de 'create_or_join_room' para clareza
def handle_join_game(data):
    player_name = data.get("player_name", "").strip()
    room_id_join = data.get("room_id", "").strip().upper()

    if not player_name or len(player_name) < 2:
        emit("error", {"message": "Nome do jogador inválido (mínimo 2 caracteres)."})
        return

    room_to_join = None
    new_room_created = False

    if not room_id_join: # Criar nova sala
        room_id_join = generate_room_code()
        rooms[room_id_join] = GameRoom(room_id_join)
        room_to_join = rooms[room_id_join]
        new_room_created = True
        print(f"Jogador {player_name} criando sala {room_id_join}")
    elif room_id_join in rooms:
        room_to_join = rooms[room_id_join]
        print(f"Jogador {player_name} tentando entrar na sala {room_id_join}")
    else:
        emit("error", {"message": "Sala não encontrada."})
        return

    if len(room_to_join.players) >= MAX_PLAYERS_PER_ROOM and request.sid not in room_to_join.players:
        emit("error", {"message": f"Sala está cheia (máximo {MAX_PLAYERS_PER_ROOM} jogadores)."})
        return

    join_room(room_id_join)
    room_to_join.add_player(request.sid, player_name)

    emit("joined_room", {
        "room_id": room_id_join,
        "player_name": player_name,
        "is_creator": new_room_created,
        "players_online": get_room_players_info(room_to_join)
    })
    
    socketio.emit("players_update", {
        "players_online": get_room_players_info(room_to_join)
    }, room=room_id_join)

    if len(room_to_join.players) == MAX_PLAYERS_PER_ROOM:
        room_to_join.game_state = "choosing_words"
        socketio.emit("start_choosing_words", {
            "message": "Ambos jogadores conectados! Por favor, escolham suas palavras."
        }, room=room_id_join)

@socketio.on("submit_words")
def handle_submit_words(data):
    sid = request.sid
    if sid not in players_online: return
    room_id = players_online[sid]["room"]
    if room_id not in rooms: return
    
    room = rooms[room_id]
    words = data.get("words", [])

    if len(words) != NUM_WORDS_PER_PLAYER:
        emit("error", {"message": f"Por favor, insira exatamente {NUM_WORDS_PER_PLAYER} palavras."})
        return
    
    for i, word_entry in enumerate(words):
        word = word_entry.strip()
        if not word:
            emit("error", {"message": f"Palavra {i+1} não pode estar vazia."})
            return
        if len(word) > MAX_WORD_LENGTH:
            emit("error", {"message": f"Palavra {i+1} muito longa (máximo {MAX_WORD_LENGTH} caracteres)."})
            return
        if len(word) < 2 : # Validação adicional de tamanho mínimo
             emit("error", {"message": f"Palavra {i+1} muito curta (mínimo 2 caracteres)."})
             return

    room.words_chosen_data[sid] = {
        'original': [w.strip() for w in words],
        'lowercase': [w.strip().lower() for w in words]
    }
    room.players[sid]["words_chosen"] = True

    socketio.emit("words_submitted_update", { # Evento mais específico
        "player_name": players_online[sid]["name"],
        "players_online": get_room_players_info(room)
    }, room=room_id)

    if room.all_players_chosen_words():
        start_game_logic(room)

def start_game_logic(room):
    room.game_state = "playing"
    sids_in_room = list(room.players.keys())
    
    player1_sid = sids_in_room[0]
    player2_sid = sids_in_room[1]

    room.game_data = {
        player1_sid: { # Dados para o Jogador 1 adivinhar (palavras do Jogador 2)
            "words_to_guess_original": room.words_chosen_data[player2_sid]['original'],
            "words_to_guess_lowercase": room.words_chosen_data[player2_sid]['lowercase'],
            "current_word_index": 0, # Começa na primeira palavra (índice 0)
            "revealed_letters_count": [1] * NUM_WORDS_PER_PLAYER, # 1 letra revelada para cada palavra inicialmente
            "completed_words_mask": [False] * NUM_WORDS_PER_PLAYER
        },
        player2_sid: { # Dados para o Jogador 2 adivinhar (palavras do Jogador 1)
            "words_to_guess_original": room.words_chosen_data[player1_sid]['original'],
            "words_to_guess_lowercase": room.words_chosen_data[player1_sid]['lowercase'],
            "current_word_index": 0,
            "revealed_letters_count": [1] * NUM_WORDS_PER_PLAYER,
            "completed_words_mask": [False] * NUM_WORDS_PER_PLAYER
        }
    }
    room.current_turn_sid = random.choice(sids_in_room)

    for sid_player in sids_in_room:
        player_game_state = room.game_data[sid_player]
        words_original = player_game_state["words_to_guess_original"]
        words_lc = player_game_state["words_to_guess_lowercase"]

        # A primeira palavra é a "palavra chave"
        first_word_given = words_original[0]
        # As iniciais das outras palavras (da segunda em diante)
        initials_of_other_words = [word[0].upper() for word in words_original[1:]]
        
        # A primeira palavra a ser efetivamente ADIVINHADA é a segunda (índice 1), mas o jogador já sabe a primeira.
        # O jogo começa com o jogador tentando "confirmar" a primeira palavra ou avançando para a segunda.
        # Para manter a lógica de "adivinhar desde a primeira palavra", mesmo que ela seja dada:
        current_target_idx = player_game_state["current_word_index"] # Começa em 0
        current_target_lc = words_lc[current_target_idx]
        revealed_count = player_game_state["revealed_letters_count"][current_target_idx] # Deve ser 1
        
        # Se a primeira palavra tem apenas 1 letra, ela é totalmente revelada pela dica inicial.
        if len(current_target_lc) <= revealed_count :
             current_hint_for_guess = current_target_lc.upper()
        else:
             current_hint_for_guess = current_target_lc[:revealed_count].upper() + "_" * (len(current_target_lc) - revealed_count)

        socketio.emit("game_started", {
            "first_word_given": first_word_given,
            "initials_of_other_words": initials_of_other_words,
            "current_hint_for_guess": current_hint_for_guess,
            "current_word_display_index": current_target_idx, # Para o front saber qual slot de palavra está ativo
            "your_turn": sid_player == room.current_turn_sid,
            "current_turn_player_name": room.players[room.current_turn_sid]["name"],
        }, room=sid_player)


@socketio.on("make_guess")
def handle_make_guess(data):
    sid = request.sid
    if sid not in players_online: return
    room_id = players_online[sid]["room"]
    if room_id not in rooms: return

    room = rooms[room_id]
    guess = data.get("guess", "").strip().lower()

    if room.game_state != "playing":
        emit("error", {"message": "O jogo não está em andamento."})
        return
    if room.current_turn_sid != sid:
        emit("error", {"message": "Não é sua vez."})
        return
    if not guess:
        emit("error", {"message": "Palpite não pode ser vazio."}) # Feedback para o jogador atual
        return

    player_game_state = room.game_data[sid]
    current_idx = player_game_state["current_word_index"]
    target_word_lc = player_game_state["words_to_guess_lowercase"][current_idx]
    target_word_original = player_game_state["words_to_guess_original"][current_idx]

    if guess == target_word_lc:
        player_game_state["completed_words_mask"][current_idx] = True
        player_game_state["current_word_index"] += 1
        
        if player_game_state["current_word_index"] >= NUM_WORDS_PER_PLAYER:
            room.game_state = "finished"
            socketio.emit("game_finished", {
                "winner_name": room.players[sid]["name"],
                "message": f"🎉 {room.players[sid]['name']} venceu o jogo!",
                "correct_sequence": player_game_state["words_to_guess_original"]
            }, room=room_id)
        else:
            # Continua jogando, prepara dica para a PRÓXIMA palavra
            next_idx = player_game_state["current_word_index"]
            next_target_lc = player_game_state["words_to_guess_lowercase"][next_idx]
            # Usa a contagem de letras reveladas para a próxima palavra (deve ser 1)
            revealed_count = player_game_state["revealed_letters_count"][next_idx] 

            if len(next_target_lc) <= revealed_count:
                 current_hint = next_target_lc.upper()
            else:
                 current_hint = next_target_lc[:revealed_count].upper() + "_" * (len(next_target_lc) - revealed_count)
            
            socketio.emit("correct_guess", {
                "player_name": room.players[sid]["name"],
                "guessed_word_original": target_word_original, # Palavra que acabou de acertar
                "next_hint": current_hint, # Dica para a próxima palavra
                "next_word_display_index": next_idx,
                "is_still_my_turn": True, # Jogador continua
                "current_turn_player_name": room.players[room.current_turn_sid]["name"],
                "completed_words_mask": player_game_state["completed_words_mask"]
            }, room=room_id) # Notifica ambos
    else:
        # Errou - revela mais uma letra da palavra ATUAL e passa a vez
        player_game_state["revealed_letters_count"][current_idx] += 1
        revealed_count = player_game_state["revealed_letters_count"][current_idx]
        
        if revealed_count >= len(target_word_lc) :
            new_hint_for_current_word = target_word_lc.upper()
        else:
            new_hint_for_current_word = target_word_lc[:revealed_count].upper() + "_" * (len(target_word_lc) - revealed_count)
        
        room.current_turn_sid = room.get_opponent_sid(sid)
        
        socketio.emit("incorrect_guess", {
            "player_name": room.players[sid]["name"], # Quem errou
            "guessed_word_attempt": data.get("guess", "").strip(), # O palpite original (não lowercase)
            "updated_hint_for_guesser": new_hint_for_current_word, # Nova dica para o jogador que errou (para o front dele)
            "word_display_index_for_guesser": current_idx, # Para o jogador que errou saber qual palavra atualizar
            "is_still_my_turn": False,
            "current_turn_player_name": room.players[room.current_turn_sid]["name"],
            "revealed_more_for_player_sid": sid, # Indica qual jogador teve mais letras reveladas
            "completed_words_mask_for_guesser": player_game_state["completed_words_mask"]
        }, room=room_id) # Notifica ambos


@socketio.on("chat_message")
def handle_chat_message(data):
    sid = request.sid
    if sid not in players_online: return
    room_id = players_online[sid]["room"]
    
    message = data.get("message", "").strip()
    if not message: return

    socketio.emit("new_chat_message", {
        "player_name": players_online[sid]["name"],
        "message": message, # Sanitizar no front-end ou aqui se for renderizar como HTML
        "timestamp": time.strftime("%H:%M")
    }, room=room_id)

@socketio.on("request_new_game") # Jogador pede novo jogo
def handle_request_new_game(data):
    sid = request.sid
    if sid not in players_online: return
    room_id = players_online[sid]["room"]
    if room_id not in rooms: return
    room = rooms[room_id]

    # Aqui você pode implementar uma lógica de votação se quiser
    # Por simplicidade, vamos resetar se um jogador pedir e o jogo estiver 'finished'
    if room.game_state == "finished":
        room.reset_game_state()
        socketio.emit("game_reset_initiated", {
             "message": f"{players_online[sid]['name']} iniciou um novo jogo! Escolham suas palavras.",
             "players_online": get_room_players_info(room)
        }, room=room_id)
        # Se ambos jogadores estão na sala, vai para escolha de palavras
        if len(room.players) == MAX_PLAYERS_PER_ROOM:
            room.game_state = "choosing_words"
            socketio.emit("start_choosing_words", {}, room=room_id)
    else:
        emit("error", {"message": "Só é possível iniciar um novo jogo após o término do atual."})


def get_room_players_info(room):
    if not room: return []
    return [{
        "name": player_data["name"],
        "words_chosen": player_data["words_chosen"],
        "is_current_turn": room.current_turn_sid == sid 
    } for sid, player_data in room.players.items()]


def cleanup_inactive_rooms():
    current_time = time.time()
    rooms_to_delete = []
    for room_id, room_obj in rooms.items():
        # Se não há jogadores E a sala tem mais de X minutos OU se a sala é muito antiga
        no_players_and_old_enough = not room_obj.players and (current_time - room_obj.created_at) > (ROOM_CLEANUP_INTERVAL_SECONDS / 2) # Ex: 5 min sem ninguém
        very_old_room = (current_time - room_obj.created_at) > ROOM_EXPIRY_SECONDS # Ex: 1 hora de idade
        
        if no_players_and_old_enough or very_old_room:
            if not room_obj.players or room_obj.game_state != "playing": # Não deletar salas ativas com jogadores
                rooms_to_delete.append(room_id)
    
    for room_id in rooms_to_delete:
        print(f"Limpando sala inativa/expirada: {room_id}")
        del rooms[room_id]

def scheduled_cleanup_task():
    with app.app_context(): # Necessário se você usar funcionalidades do app Flask aqui
        while True:
            socketio.sleep(ROOM_CLEANUP_INTERVAL_SECONDS) 
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Executando limpeza de salas agendada...")
            cleanup_inactive_rooms()
            print(f"Salas ativas após limpeza: {len(rooms)}")

if __name__ == "__main__":
    print("Iniciando tarefa de limpeza de salas em background...")
    eventlet.spawn(scheduled_cleanup_task)
    
    port = int(os.environ.get("PORT", 5000))
    print(f"Servidor Socket.IO iniciando em host 0.0.0.0 porta {port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=True) # debug=True para desenvolvimento
