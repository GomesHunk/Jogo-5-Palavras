import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import time
import os
import random
import string

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

rooms = {}
players_online = {}

def generate_room_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

class GameRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = {}
        self.words_chosen = {}
        self.current_turn = None
        self.game_state = "waiting"
        self.game_data = None
        self.created_at = time.time()

    def reset_game(self):
        self.words_chosen = {}
        self.current_turn = None
        self.game_state = "waiting"
        self.game_data = None

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("connect")
def on_connect():
    emit("connected", {"message": "Conectado ao servidor."})

@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    if sid in players_online:
        room_id = players_online[sid]['room']
        player_name = players_online[sid]['name']
        leave_room(room_id)
        del players_online[sid]
        if room_id in rooms and sid in rooms[room_id].players:
            del rooms[room_id].players[sid]
            socketio.emit("player_left", {
                "player_name": player_name,
                "players_online": get_room_players(room_id)
            }, room=room_id)

# Evento corrigido para match com frontend
@socketio.on("join_game")
def join_game(data):
    player_name = data.get("player_name")
    room_id = data.get("room_id")
    
    if not player_name:
        emit("error", {"message": "Nome do jogador é obrigatório"})
        return

    # Se não tem room_id, cria uma nova sala
    if not room_id:
        room_id = generate_room_code()
        rooms[room_id] = GameRoom(room_id)
    
    # Se a sala não existe, cria
    if room_id not in rooms:
        rooms[room_id] = GameRoom(room_id)
    
    # Limita a 2 jogadores por sala
    if len(rooms[room_id].players) >= 2:
        emit("error", {"message": "Sala está cheia (máximo 2 jogadores)"})
        return

    join_room(room_id)
    players_online[request.sid] = {"name": player_name, "room": room_id}
    rooms[room_id].players[request.sid] = {"name": player_name, "words_chosen": False}

    emit("joined_room", {
        "room_id": room_id,
        "player_name": player_name,
        "players_online": get_room_players(room_id)
    })
    
    # Notifica outros jogadores na sala
    socketio.emit("players_update", {
        "players_online": get_room_players(room_id)
    }, room=room_id)

@socketio.on("chat_message")
def chat_message(data):
    sid = request.sid
    if sid not in players_online:
        return
    room_id = players_online[sid]["room"]
    player_name = players_online[sid]["name"]
    message = data.get("message")
    
    if not message or not message.strip():
        return
        
    socketio.emit("new_chat_message", {
        "player_name": player_name,
        "message": message.strip(),
        "timestamp": time.strftime("%H:%M")
    }, room=room_id)

@socketio.on("submit_words")
def submit_words(data):
    sid = request.sid
    if sid not in players_online:
        return
    room_id = players_online[sid]["room"]
    room = rooms[room_id]
    words = data.get("words", [])
    
    # Validações
    if len(words) != 5:
        emit("error", {"message": "Insira exatamente 5 palavras."})
        return
    
    # Valida se todas as palavras foram preenchidas e não são muito grandes
    for i, word in enumerate(words):
        if not word or not word.strip():
            emit("error", {"message": f"Palavra {i+1} não pode estar vazia"})
            return
        if len(word.strip()) > 50:
            emit("error", {"message": f"Palavra {i+1} muito longa (máximo 50 caracteres)"})
            return

    # Salva as palavras (mantém capitalização original mas salva em lowercase para comparação)
    room.words_chosen[sid] = {
        'original': [word.strip() for word in words],
        'lowercase': [word.strip().lower() for word in words]
    }
    room.players[sid]["words_chosen"] = True

    socketio.emit("words_submitted", {
        "player_name": players_online[sid]["name"],
        "waiting_for": len(room.players) - len(room.words_chosen),
        "players_online": get_room_players(room_id)
    }, room=room_id)

    # Inicia o jogo quando ambos jogadores submeteram palavras
    if len(room.words_chosen) == 2:
        start_game(room_id)

def start_game(room_id):
    room = rooms[room_id]
    sids = list(room.words_chosen.keys())
    
    # Cada jogador vai tentar adivinhar as palavras do oponente
    opponent_words = {
        sids[0]: room.words_chosen[sids[1]],  # Player 1 adivinha palavras do Player 2
        sids[1]: room.words_chosen[sids[0]],  # Player 2 adivinha palavras do Player 1
    }
    
    # Define quem começa (aleatório)
    room.current_turn = random.choice(sids)
    
    # Inicializa dados do jogo
    room.game_data = {
        "words": opponent_words,  # Palavras que cada jogador deve adivinhar
        "current_word_index": {sid: 0 for sid in sids},  # Índice da palavra atual de cada jogador
        "revealed_letters": {sid: [1, 1, 1, 1, 1] for sid in sids},  # Quantas letras reveladas de cada palavra
        "completed": {sid: [False] * 5 for sid in sids}  # Palavras já completadas
    }
    
    room.game_state = "playing"

    # Prepara as dicas iniciais para cada jogador
    for sid in sids:
        opponent_sid = [k for k in sids if k != sid][0]
        initial_hints = []
        target_words = room.game_data["words"][sid]['lowercase']
        
        for i, word in enumerate(target_words):
            if len(word) > 0:
                initial_hints.append(word[0].upper() + "_" * (len(word) - 1))
            else:
                initial_hints.append("_")
        
        # Envia dados iniciais para cada jogador
        emit("game_started", {
            "keyword": room.game_data["words"][sid]['original'][0],  # Palavra-chave (primeira palavra)
            "hint_letters": initial_hints,  # Primeiras letras de cada palavra
            "current_turn": room.players[room.current_turn]["name"],
            "your_turn": sid == room.current_turn,
            "current_word_index": 0
        }, room=sid)

@socketio.on("make_guess")
def make_guess(data):
    sid = request.sid
    guess = data.get("guess", "").strip()
    
    if sid not in players_online:
        return

    room_id = players_online[sid]["room"]
    room = rooms[room_id]

    if room.current_turn != sid:
        emit("error", {"message": "Não é sua vez."})
        return
    
    if room.game_state != "playing":
        emit("error", {"message": "Jogo não está ativo."})
        return

    current_idx = room.game_data["current_word_index"][sid]
    target_words = room.game_data["words"][sid]['lowercase']
    target_word_original = room.game_data["words"][sid]['original'][current_idx]
    correct_word = target_words[current_idx]

    if guess.lower() == correct_word:
        # Acertou!
        room.game_data["completed"][sid][current_idx] = True
        room.game_data["current_word_index"][sid] += 1
        
        # Verifica se completou todas as 5 palavras
        if room.game_data["current_word_index"][sid] >= 5:
            socketio.emit("game_finished", {
                "winner": players_online[sid]["name"],
                "message": f"🏆 {players_online[sid]['name']} venceu o jogo!",
                "completed_sequence": room.game_data["words"][sid]['original']
            }, room=room_id)
            room.game_state = "finished"
        else:
            # Continua jogando (mesma pessoa joga novamente)
            next_word_idx = room.game_data["current_word_index"][sid]
            next_target_word = target_words[next_word_idx]
            revealed_count = room.game_data["revealed_letters"][sid][next_word_idx]
            
            if len(next_target_word) > revealed_count:
                current_hint = next_target_word[:revealed_count].upper() + "_" * (len(next_target_word) - revealed_count)
            else:
                current_hint = next_target_word.upper()
            
            socketio.emit("correct_guess", {
                "player_name": players_online[sid]["name"],
                "guessed_word": target_word_original,
                "next_turn": players_online[room.current_turn]["name"],
                "hint": current_hint,
                "current_word_index": next_word_idx,
                "continues_playing": True
            }, room=room_id)
    else:
        # Errou - revela mais uma letra e passa a vez
        current_idx = room.game_data["current_word_index"][sid]
        target_word = target_words[current_idx]
        
        # Aumenta o número de letras reveladas
        room.game_data["revealed_letters"][sid][current_idx] += 1
        revealed_count = room.game_data["revealed_letters"][sid][current_idx]
        
        # Gera a nova dica
        if revealed_count >= len(target_word):
            new_hint = target_word.upper()
        else:
            new_hint = target_word[:revealed_count].upper() + "_" * (len(target_word) - revealed_count)
        
        # Passa a vez para o outro jogador
        advance_turn(room)
        
        socketio.emit("incorrect_guess", {
            "player_name": players_online[sid]["name"],
            "guess": guess,
            "hint": new_hint,
            "next_turn": players_online[room.current_turn]["name"],
            "revealed_more": True
        }, room=room_id)

@socketio.on("new_game")
def new_game():
    sid = request.sid
    if sid not in players_online:
        return
    room_id = players_online[sid]["room"]
    room = rooms[room_id]
    
    # Reset do jogo
    room.reset_game()
    
    # Reset dos jogadores
    for player_sid in room.players:
        room.players[player_sid]["words_chosen"] = False
    
    socketio.emit("game_reset", {
        "message": "Novo jogo iniciado. Escolham suas palavras novamente!",
        "players_online": get_room_players(room_id)
    }, room=room_id)

def advance_turn(room):
    sids = list(room.players.keys())
    current_idx = sids.index(room.current_turn)
    room.current_turn = sids[(current_idx + 1) % len(sids)]

def get_room_players(room_id):
    if room_id not in rooms:
        return []
    
    room = rooms[room_id]
    return [{
        "name": room.players[sid]["name"],
        "words_chosen": room.players[sid]["words_chosen"],
        "is_current_turn": room.current_turn == sid if room.current_turn else False
    } for sid in room.players]

# Limpeza automática de salas vazias
def cleanup_empty_rooms():
    current_time = time.time()
    rooms_to_delete = []
    
    for room_id, room in rooms.items():
        # Remove salas sem jogadores ou muito antigas (1 hora)
        if not room.players or (current_time - room.created_at) > 3600:
            rooms_to_delete.append(room_id)
    
    for room_id in rooms_to_delete:
        del rooms[room_id]

# Chama limpeza a cada 10 minutos
@socketio.on("cleanup_trigger")
def trigger_cleanup():
    cleanup_empty_rooms()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
