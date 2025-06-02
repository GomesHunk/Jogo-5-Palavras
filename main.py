from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room
import time
import os
import eventlet
import random
import string

eventlet.monkey_patch()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

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

@socketio.on("create_room")
def create_room(data):
    player_name = data.get("player_name")
    if not player_name:
        emit("error", {"message": "Nome do jogador é obrigatório"})
        return

    room_code = generate_room_code()
    rooms[room_code] = GameRoom(room_code)
    join_room(room_code)

    players_online[request.sid] = {"name": player_name, "room": room_code}
    rooms[room_code].players[request.sid] = {"name": player_name, "words_chosen": False}

    emit("room_created", {
        "room_id": room_code,
        "player_name": player_name,
        "players_online": get_room_players(room_code)
    }, room=request.sid)

@socketio.on("join_room_code")
def join_room_code(data):
    room_code = data.get("room_code")
    player_name = data.get("player_name")

    if room_code not in rooms:
        emit("error", {"message": "Sala não encontrada"})
        return

    join_room(room_code)
    players_online[request.sid] = {"name": player_name, "room": room_code}
    rooms[room_code].players[request.sid] = {"name": player_name, "words_chosen": False}

    socketio.emit("player_joined", {
        "player_name": player_name,
        "players_online": get_room_players(room_code)
    }, room=room_code)

@socketio.on("chat_message")
def chat_message(data):
    sid = request.sid
    if sid not in players_online:
        return
    room_id = players_online[sid]["room"]
    player_name = players_online[sid]["name"]
    message = data.get("message")
    socketio.emit("new_chat_message", {
        "player_name": player_name,
        "message": message,
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
    if len(words) != 5:
        emit("error", {"message": "Insira exatamente 5 palavras."})
        return

    room.words_chosen[sid] = [word.lower() for word in words]
    room.players[sid]["words_chosen"] = True

    socketio.emit("words_submitted", {
        "player_name": players_online[sid]["name"],
        "waiting_for": len(room.players) - len(room.words_chosen),
        "players_online": get_room_players(room_id)
    }, room=room_id)

    if len(room.words_chosen) == 2:
        start_game(room_id)

def start_game(room_id):
    room = rooms[room_id]
    sids = list(room.words_chosen.keys())
    opponent_words = {
        sids[0]: room.words_chosen[sids[1]],
        sids[1]: room.words_chosen[sids[0]],
    }
    room.current_turn = sids[0]
    room.game_data = {
        "words": opponent_words,
        "revealed": {sid: [""] * 5 for sid in sids},
        "current": {sid: 0 for sid in sids},
        "done": {sid: False for sid in sids}
    }
    room.game_state = "playing"

    socketio.emit("game_started", {
        "current_turn": room.players[room.current_turn]["name"],
        "players_online": get_room_players(room_id)
    }, room=room_id)

@socketio.on("make_guess")
def make_guess(data):
    sid = request.sid
    guess = data.get("guess", "").strip().lower()
    if sid not in players_online:
        return

    room_id = players_online[sid]["room"]
    room = rooms[room_id]

    if room.current_turn != sid:
        emit("error", {"message": "Não é sua vez."})
        return

    opponent_sid = [k for k in room.players if k != sid][0]
    target_words = room.game_data["words"][sid]
    idx = room.game_data["current"][sid]
    correct_word = target_words[idx]

    if guess == correct_word:
        room.game_data["current"][sid] += 1
        if room.game_data["current"][sid] >= 5:
            room.game_data["done"][sid] = True
            socketio.emit("game_finished", {
                "winner": players_online[sid]["name"],
                "message": f"🏆 {players_online[sid]['name']} venceu o jogo!"
            }, room=room_id)
        else:
            advance_turn(room)
            socketio.emit("correct_guess", {
                "player_name": players_online[sid]["name"],
                "word": correct_word.upper(),
                "next_turn": players_online[room.current_turn]["name"]
            }, room=room_id)
    else:
        hint_len = len(room.game_data["revealed"][sid][idx])
        next_hint = correct_word[:hint_len+1].upper()
        room.game_data["revealed"][sid][idx] = next_hint
        advance_turn(room)
        socketio.emit("incorrect_guess", {
            "player_name": players_online[sid]["name"],
            "guess": guess.upper(),
            "hint": next_hint,
            "next_turn": players_online[room.current_turn]["name"]
        }, room=room_id)

@socketio.on("new_game")
def new_game():
    sid = request.sid
    if sid not in players_online:
        return
    room_id = players_online[sid]["room"]
    room = rooms[room_id]
    room.reset_game()
    socketio.emit("game_reset", {
        "message": "Novo jogo iniciado.",
        "players_online": get_room_players(room_id)
    }, room=room_id)

def advance_turn(room):
    sids = list(room.players.keys())
    idx = sids.index(room.current_turn)
    room.current_turn = sids[(idx + 1) % len(sids)]

def get_room_players(room_id):
    room = rooms[room_id]
    return [{
        "name": room.players[sid]["name"],
        "words_chosen": room.players[sid]["words_chosen"],
        "is_current_turn": room.current_turn == sid
    } for sid in room.players]

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
