import eventlet
eventlet.monkey_patch()

from flask import Flask, request, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import time
import os
import random
import string

# --- Constantes ---
NUM_WORDS = 5
MAX_PLAYERS = 2
WORD_MIN_LEN, WORD_MAX_LEN = 2, 25
ROOM_CODE_LEN = 6
CLEANUP_INTERVAL = 600  # 10 minutos
ROOM_EXPIRY = 3600      # 1 hora

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret_dev_key!')
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

SESSIONS = {} # sid: player_name (para tracking simples de nomes por sessão)
ROOMS = {}    # room_id: GameRoom instance

class GameRoom:
    def __init__(self, room_id_val):
        self.id = room_id_val
        self.players = {}  # sid: { name: str, words_original: [], words_lc: [], ready: bool }
        self.game_state = "waiting" # waiting, choosing_words, playing, finished
        self.turn_sid = None
        # player_view_data[sid_of_player_whose_view_it_is]
        self.player_view_data = {} # sid: { target_words_orig: [], target_words_lc: [], completed_mask: [], current_guess_idx: int, revealed_counts: [] }
        self.created_at = time.time()
        self.last_action_summary = None # Para toasts no frontend

    def add_player(self, sid, name):
        if len(self.players) < MAX_PLAYERS and sid not in self.players:
            self.players[sid] = {"name": name, "words_original": [], "words_lc": [], "ready": False}
            return True
        return False

    def remove_player(self, sid):
        return self.players.pop(sid, None)

    def set_player_words(self, sid, words_original_list):
        if sid in self.players:
            self.players[sid]["words_original"] = words_original_list
            self.players[sid]["words_lc"] = [w.lower() for w in words_original_list]
            self.players[sid]["ready"] = True

    def are_all_players_ready(self):
        return len(self.players) == MAX_PLAYERS and all(p['ready'] for p in self.players.values())

    def get_opponent_sid(self, player_sid):
        return next((sid for sid in self.players if sid != player_sid), None)

    def setup_player_challenges(self):
        sids = list(self.players.keys())
        p1_sid, p2_sid = sids[0], sids[1]

        self.player_view_data[p1_sid] = { # p1 guesses p2's words
            "target_words_orig": self.players[p2_sid]["words_original"],
            "target_words_lc": self.players[p2_sid]["words_lc"],
            "completed_mask": [True] + [False] * (NUM_WORDS - 1), # Keyword is "given"
            "current_guess_idx": 1, # Start guessing the word at index 1
            "revealed_counts": [len(self.players[p2_sid]["words_original"][0])] + [1] * (NUM_WORDS - 1)
        }
        self.player_view_data[p2_sid] = { # p2 guesses p1's words
            "target_words_orig": self.players[p1_sid]["words_original"],
            "target_words_lc": self.players[p1_sid]["words_lc"],
            "completed_mask": [True] + [False] * (NUM_WORDS - 1),
            "current_guess_idx": 1,
            "revealed_counts": [len(self.players[p1_sid]["words_original"][0])] + [1] * (NUM_WORDS - 1)
        }

    def reset_for_new_round(self):
        for sid_player in self.players:
            self.players[sid_player].update({"words_original": [], "words_lc": [], "ready": False})
        self.game_state = "choosing_words" if len(self.players) == MAX_PLAYERS else "waiting"
        self.turn_sid = None
        self.player_view_data = {}
        self.last_action_summary = None


def generate_id(length=ROOM_CODE_LEN):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def get_player_list_for_room(room_obj):
    if not room_obj: return []
    return [{
        "name": p_data["name"],
        "ready": p_data["ready"], # "ready" now means words submitted
        "is_turn": room_obj.turn_sid == sid
    } for sid, p_data in room_obj.players.items()]

def broadcast_unified_game_view(room_obj, action_summary=None):
    if not room_obj or room_obj.game_state != "playing" or not room_obj.turn_sid:
        return

    active_sid = room_obj.turn_sid
    if active_sid not in room_obj.players: return # Safety check
        
    active_player_name = room_obj.players[active_sid]["name"]
    active_player_view = room_obj.player_view_data.get(active_sid)

    if not active_player_view: return # Should not happen if game is setup

    keyword = active_player_view["target_words_orig"][0]
    initials = [w[0].upper() for w in active_player_view["target_words_orig"][1:]]
    
    current_idx = active_player_view["current_guess_idx"]
    hint = "SEQUÊNCIA COMPLETA!"
    if current_idx < NUM_WORDS:
        word_lc = active_player_view["target_words_lc"][current_idx]
        revealed = active_player_view["revealed_counts"][current_idx]
        hint = generate_hint(word_lc, revealed)
    else: # All words for current player guessed
        current_idx = NUM_WORDS 

    payload_to_all = {
        "active_player_display_name": active_player_name, # Whose challenge is being viewed
        "keyword_for_display": keyword,
        "initials_for_display": initials,
        "all_target_words_for_progress_slots": active_player_view["target_words_orig"],
        "completed_mask_for_progress_slots": active_player_view["completed_mask"],
        "active_word_idx_for_hint": current_idx, # Which word the main hint refers to
        "main_hint_for_active_word": hint,
        "last_action": action_summary or room_obj.last_action_summary
    }
    room_obj.last_action_summary = None # Clear after sending

    for sid_client in room_obj.players.keys():
        client_specific_payload = payload_to_all.copy()
        client_specific_payload["is_your_turn"] = (sid_client == active_sid)
        emit("game_view_update", client_specific_payload, room=sid_client)

@app.route("/")
def home():
    return render_template("index.html")

@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")
    emit('connected_to_server', {'message': 'Successfully connected!'})

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f"Client disconnected: {request.sid}")
    player_name = SESSIONS.pop(sid, None)
    
    # Find which room the player was in to notify others
    room_to_update_id = None
    for r_id, r_obj in list(ROOMS.items()): # list() for safe iteration if modifying ROOMS
        if sid in r_obj.players:
            room_to_update_id = r_id
            r_obj.remove_player(sid)
            if player_name: # Ensure a name was associated
                emit("player_left_room", {"name": player_name, "players": get_player_list_for_room(r_obj)}, room=r_id)

            if r_obj.game_state == "playing" and len(r_obj.players) == 1:
                opponent_sid = r_obj.get_opponent_sid(None) # Should be the only one left
                if opponent_sid:
                    opponent_name = r_obj.players[opponent_sid]["name"]
                    r_obj.game_state = "finished"
                    emit("game_over", {
                        "winner": opponent_name,
                        "reason": f"{player_name or 'Oponente'} desconectou."
                    }, room=opponent_sid) # Notify only the winner
            elif not r_obj.players: # Room is empty
                print(f"Room {r_id} is now empty. Will be cleaned up.")
            break 

@socketio.on('create_or_join_room')
def on_create_or_join_room(data):
    sid = request.sid
    player_name = data.get('name', '').strip()
    room_id_join = data.get('room_id', '').strip().upper()

    if not player_name or len(player_name) < 2 or len(player_name) > 20:
        emit('error_event', {'message': 'Nome inválido (2-20 caracteres).'}); return
    
    SESSIONS[sid] = player_name # Track player name with session

    target_room = None
    if not room_id_join: # Create new room
        room_id_join = generate_id()
        while room_id_join in ROOMS: room_id_join = generate_id() # Ensure unique
        target_room = GameRoom(room_id_join)
        ROOMS[room_id_join] = target_room
        print(f"Player {player_name} created room {room_id_join}")
    elif room_id_join in ROOMS:
        target_room = ROOMS[room_id_join]
    else:
        emit('error_event', {'message': 'Sala não encontrada.'}); return

    if not target_room.add_player(sid, player_name):
        emit('error_event', {'message': 'Não foi possível entrar na sala (cheia ou erro).'}); return

    join_room(room_id_join)
    emit('joined_room_success', {'room_id': room_id_join, 'player_name': player_name})
    emit("players_update", {"players": get_player_list_for_room(target_room)}, room=room_id_join)

    if len(target_room.players) == MAX_PLAYERS and target_room.game_state == "waiting":
        target_room.game_state = "choosing_words"
        emit("all_players_joined_choose_words", room=room_id_join)

@socketio.on('submit_player_words')
def on_submit_player_words(data):
    sid = request.sid
    player_name = SESSIONS.get(sid)
    if not player_name: emit('error_event', {'message': 'Sessão inválida.'}); return

    room_id = next((r_id for r_id, r in ROOMS.items() if sid in r.players), None)
    if not room_id: emit('error_event', {'message': 'Você não está em uma sala.'}); return
    
    room = ROOMS[room_id]
    words = data.get('words', [])
    
    if len(words) != NUM_WORDS:
        emit('error_event', {'message': f'Envie {NUM_WORDS} palavras.'}); return
    for i, w in enumerate(words):
        if not w.strip() or len(w.strip()) < WORD_MIN_LEN or len(w.strip()) > WORD_MAX_LEN:
            emit('error_event', {'message': f'Palavra {i+1} inválida.'}); return
    
    room.set_player_words(sid, [w.strip() for w in words])
    emit("players_update", {"players": get_player_list_for_room(room)}, room=room_id) # Update ready status

    if room.are_all_players_ready():
        room.setup_player_challenges()
        room.turn_sid = random.choice(list(room.players.keys()))
        room.game_state = "playing"
        room.last_action_summary = {"type": "game_start", "starting_player": room.players[room.turn_sid]["name"]}
        broadcast_unified_game_view(room)

@socketio.on('player_guess')
def on_player_guess(data):
    sid = request.sid
    player_name = SESSIONS.get(sid)
    if not player_name: return
    room_id = next((r_id for r_id, r in ROOMS.items() if sid in r.players), None)
    if not room_id: return
    
    room = ROOMS[room_id]
    if room.game_state != "playing" or room.turn_sid != sid:
        emit('error_event', {'message': 'Não é sua vez ou jogo inativo.'}); return

    guess = data.get('guess', '').strip()
    if not guess: emit('error_event', {'message': 'Palpite vazio.'}); return

    player_challenge = room.player_view_data[sid]
    idx_to_guess = player_challenge["current_guess_idx"]

    if idx_to_guess >= NUM_WORDS: # Should not happen if game ends properly
        broadcast_unified_game_view(room); return

    target_word = player_challenge["target_words_lc"][idx_to_guess]
    original_word = player_challenge["target_words_orig"][idx_to_guess]

    if guess.lower() == target_word:
        player_challenge["completed_mask"][idx_to_guess] = True
        player_challenge["current_guess_idx"] += 1
        room.last_action_summary = {"type": "correct", "player": player_name, "word": original_word}
        
        if all(player_challenge["completed_mask"][1:]): # Check if all guessable words are done
            room.game_state = "finished"
            emit("game_over", {
                "winner": player_name, 
                "reason": "Completou todas as palavras!",
                "sequence": player_challenge["target_words_orig"]
            }, room=room_id) # Send to all in room
            return
    else: # Incorrect guess
        player_challenge["revealed_counts"][idx_to_guess] = min(
            len(target_word), 
            player_challenge["revealed_counts"][idx_to_guess] + 1
        )
        room.turn_sid = room.get_opponent_sid(sid)
        room.last_action_summary = {"type": "incorrect", "player": player_name, "attempt": guess}
    
    broadcast_unified_game_view(room)

@socketio.on('send_chat_message')
def on_send_chat_message(data):
    sid = request.sid
    player_name = SESSIONS.get(sid)
    if not player_name: return
    room_id = next((r_id for r_id, r in ROOMS.items() if sid in r.players), None)
    if not room_id: return
    
    message = data.get('message', '').strip()
    if 0 < len(message) <= 150:
        emit('new_chat_message_broadcast', {
            'name': player_name, 
            'text': message, 
            'time': time.strftime('%H:%M')
        }, room=room_id)

@socketio.on('request_play_again')
def on_request_play_again(data):
    sid = request.sid
    player_name = SESSIONS.get(sid)
    if not player_name: return
    room_id = next((r_id for r_id, r in ROOMS.items() if sid in r.players), None)
    if not room_id: return
    
    room = ROOMS[room_id]
    if room.game_state == "finished":
        # Simplificado: um jogador pede, reseta para todos na sala.
        room.reset_for_new_round()
        emit("play_again_initiated", {"message": f"{player_name} iniciou uma nova rodada!"}, room=room_id)
        if len(room.players) == MAX_PLAYERS:
             emit("all_players_joined_choose_words", room=room_id) # Vai para escolha de palavras
    else:
        emit('error_event', {'message': 'Jogo atual ainda não terminou.'})

@socketio.on('player_leaving_room') # Se o jogador clica em "Sair"
def on_player_leaving_room():
    sid = request.sid
    player_name = SESSIONS.pop(sid, "Alguém") # Remove da sessão global também
    
    room_id_left = None
    for r_id, r_obj in list(ROOMS.items()):
        if sid in r_obj.players:
            room_id_left = r_id
            r_obj.remove_player(sid)
            leave_room(r_id) # SocketIO's specific leave
            emit("player_left_room", {"name": player_name, "players": get_player_list_for_room(r_obj)}, room=r_id)
            
            if r_obj.game_state == "playing" and len(r_obj.players) == 1:
                opponent_sid = r_obj.get_opponent_sid(None)
                if opponent_sid:
                    opponent_name = r_obj.players[opponent_sid]["name"]
                    r_obj.game_state = "finished"
                    emit("game_over", {"winner": opponent_name, "reason": f"{player_name} saiu do jogo."}, room=opponent_sid)
            elif not r_obj.players:
                print(f"Room {r_id} está vazia após {player_name} sair.")
            break 
    print(f"Jogador {player_name} (SID: {sid}) saiu da sala {room_id_left if room_id_left else 'desconhecida'}.")


def cleanup_task():
    with app.app_context():
        while True:
            socketio.sleep(CLEANUP_INTERVAL)
            print(f"[{time.strftime('%H:%M:%S')}] Executando limpeza de salas...")
            current_time = time.time()
            for r_id in list(ROOMS.keys()):
                room = ROOMS[r_id]
                is_old = (current_time - room.created_at) > ROOM_EXPIRY
                is_empty_and_stale = not room.players and (current_time - room.created_at > 300) # Vazia por 5 min
                
                if (is_old and room.game_state != "playing") or is_empty_and_stale:
                    print(f"Limpando sala {r_id}.")
                    del ROOMS[r_id]
            print(f"Salas ativas: {len(ROOMS)}")

if __name__ == '__main__':
    print("Iniciando tarefa de limpeza em background...")
    eventlet.spawn(cleanup_task)
    port = int(os.environ.get("PORT", 5000))
    print(f"Servidor Flask-SocketIO iniciando na porta {port}...")
    socketio.run(app, host='0.0.0.0', port=port, debug=True, use_reloader=False)
