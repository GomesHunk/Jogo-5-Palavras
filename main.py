
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
import time
import eventlet

eventlet.monkey_patch()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Estruturas de dados para multiplayer
sessions = {}
rooms = {}
players_online = {}

class GameRoom:
    def __init__(self, room_id):
        self.room_id = room_id
        self.players = {}
        self.game_state = "waiting"  # waiting, playing
        self.current_turn = None
        self.words_chosen = {}
        self.game_data = None
        self.created_at = time.time()

@socketio.on('connect')
def handle_connect():
    print(f'Cliente conectado: {request.sid}')
    emit('connected', {'message': 'Conectado com sucesso!'})

@socketio.on('disconnect')
def handle_disconnect():
    player_id = request.sid
    # Remove player from online list
    if player_id in players_online:
        player_name = players_online[player_id]['name']
        room_id = players_online[player_id]['room']
        del players_online[player_id]
        
        # Notify room about player leaving
        if room_id in rooms:
            socketio.emit('player_left', {
                'player_name': player_name,
                'players_online': get_room_players(room_id)
            }, room=room_id)
        
        print(f'Cliente desconectado: {player_id}')

@socketio.on('join_game')
def handle_join_game(data):
    player_name = data.get('player_name')
    room_id = data.get('room_id', 'default_room')
    
    if not player_name:
        emit('error', {'message': 'Nome do jogador é obrigatório'})
        return
    
    # Create room if doesn't exist
    if room_id not in rooms:
        rooms[room_id] = GameRoom(room_id)
    
    room = rooms[room_id]
    player_id = request.sid
    
    # Add player to room
    join_room(room_id)
    players_online[player_id] = {
        'name': player_name,
        'room': room_id,
        'socket_id': player_id
    }
    
    room.players[player_id] = {
        'name': player_name,
        'words_chosen': False,
        'socket_id': player_id
    }
    
    # Notify all players in room
    socketio.emit('player_joined', {
        'player_name': player_name,
        'players_online': get_room_players(room_id),
        'game_state': room.game_state
    }, room=room_id)
    
    emit('joined_room', {
        'room_id': room_id,
        'players_online': get_room_players(room_id),
        'game_state': room.game_state
    })

@socketio.on('chat_message')
def handle_chat_message(data):
    player_id = request.sid
    if player_id not in players_online:
        emit('error', {'message': 'Você não está em uma sala'})
        return
    
    player_info = players_online[player_id]
    room_id = player_info['room']
    
    message_data = {
        'player_name': player_info['name'],
        'message': data.get('message', ''),
        'timestamp': time.strftime('%H:%M:%S')
    }
    
    socketio.emit('new_chat_message', message_data, room=room_id)

@socketio.on('submit_words')
def handle_submit_words(data):
    player_id = request.sid
    if player_id not in players_online:
        emit('error', {'message': 'Você não está em uma sala'})
        return
    
    player_info = players_online[player_id]
    room_id = player_info['room']
    room = rooms[room_id]
    
    words = data.get('words', [])
    if len(words) != 6:
        emit('error', {'message': 'Você deve enviar exatamente 6 palavras'})
        return
    
    # Store player's words
    room.words_chosen[player_id] = words
    room.players[player_id]['words_chosen'] = True
    
    # Check if both players have chosen words
    if len(room.words_chosen) >= 2:
        # Start game
        room.game_state = "playing"
        player_ids = list(room.words_chosen.keys())
        room.current_turn = player_ids[0]  # First player starts
        
        # Initialize game for both players
        combined_words = []
        for pid in player_ids:
            combined_words.extend(room.words_chosen[pid])
        
        # Use first 6 words for the game
        game_words = combined_words[:6]
        
        # Create game session
        room.game_data = {
            "words": [word.lower() for word in game_words[1:]],
            "hint_letters": [word[0].upper() for word in game_words[1:]],
            "revealed_letters": [word[0].upper() for word in game_words[1:]],
            "current": 0,
            "done": False,
            "first_word": game_words[0]
        }
        
        # Notify all players that game started
        socketio.emit('game_started', {
            'first_word': game_words[0],
            'current_turn': room.players[room.current_turn]['name'],
            'hint_letters': room.game_data['hint_letters'],
            'players_online': get_room_players(room_id)
        }, room=room_id)
    else:
        # Notify that player submitted words
        socketio.emit('words_submitted', {
            'player_name': player_info['name'],
            'waiting_for': len(room.players) - len(room.words_chosen),
            'players_online': get_room_players(room_id)
        }, room=room_id)

@socketio.on('make_guess')
def handle_make_guess(data):
    player_id = request.sid
    if player_id not in players_online:
        emit('error', {'message': 'Você não está em uma sala'})
        return
    
    player_info = players_online[player_id]
    room_id = player_info['room']
    room = rooms[room_id]
    
    # Check if it's player's turn
    if room.current_turn != player_id:
        emit('error', {'message': 'Não é sua vez!'})
        return
    
    if room.game_state != "playing":
        emit('error', {'message': 'O jogo não está ativo'})
        return
    
    guess = data.get('guess', '').strip().lower()
    game_data = room.game_data
    
    idx = game_data['current']
    correct_word = game_data['words'][idx]
    
    if guess == correct_word:
        game_data['current'] += 1
        
        if game_data['current'] == 5:
            game_data['done'] = True
            socketio.emit('game_finished', {
                'winner': player_info['name'],
                'message': f"🏆 {player_info['name']} venceu o jogo!"
            }, room=room_id)
        else:
            # Switch turns
            player_ids = list(room.players.keys())
            current_idx = player_ids.index(room.current_turn)
            room.current_turn = player_ids[(current_idx + 1) % len(player_ids)]
            
            socketio.emit('correct_guess', {
                'player_name': player_info['name'],
                'word': correct_word.upper(),
                'next_turn': room.players[room.current_turn]['name'],
                'current_word_index': game_data['current'],
                'hint': game_data['hint_letters'][game_data['current']] if game_data['current'] < 5 else None
            }, room=room_id)
    else:
        # Reveal one more letter
        current_word = game_data['words'][idx]
        revealed = game_data['revealed_letters'][idx]
        
        if len(revealed) < len(current_word):
            game_data['revealed_letters'][idx] = current_word[:len(revealed)+1].upper()
        
        # Switch turns
        player_ids = list(room.players.keys())
        current_idx = player_ids.index(room.current_turn)
        room.current_turn = player_ids[(current_idx + 1) % len(player_ids)]
        
        socketio.emit('incorrect_guess', {
            'player_name': player_info['name'],
            'guess': guess.upper(),
            'hint': game_data['revealed_letters'][idx],
            'next_turn': room.players[room.current_turn]['name']
        }, room=room_id)

def get_room_players(room_id):
    if room_id not in rooms:
        return []
    
    room = rooms[room_id]
    players = []
    for player_id, player_data in room.players.items():
        players.append({
            'name': player_data['name'],
            'words_chosen': player_data['words_chosen'],
            'is_current_turn': room.current_turn == player_id if room.game_state == "playing" else False
        })
    return players

# Keep original REST endpoints for compatibility
@app.route("/start", methods=["POST"])
def start_game():
    data = request.json
    player = data.get("player")
    word_list = data.get("words")

    if not player or not isinstance(player, str):
        return jsonify({"error": "Invalid input: 'player' must be a non-empty string."}), 400
    if not isinstance(word_list, list) or len(word_list) != 6:
        return jsonify({"error": "Invalid input: 'words' must be a list of exactly 6 words."}), 400
    if not all(isinstance(word, str) and word for word in word_list):
         return jsonify({"error": "Invalid input: 'words' list must contain only non-empty strings."}), 400

    sessions[player] = {
        "words": [word.lower() for word in word_list[1:]],
        "hint_letters": [word[0].upper() for word in word_list[1:]],
        "revealed_letters": [word[0].upper() for word in word_list[1:]],
        "current": 0,
        "done": False
    }

    return jsonify({
        "status": "success",
        "message": f"Jogo iniciado para {player}.",
        "first_word": word_list[0],
        "hint_letters": sessions[player]['hint_letters']
    }), 201

@app.route("/guess", methods=["POST"])
def guess_word():
    data = request.json
    player = data.get("player")
    guess = data.get("guess")

    if not player or not isinstance(player, str):
        return jsonify({"error": "Invalid input: 'player' must be a non-empty string."}), 400
    if not isinstance(guess, str):
         return jsonify({"error": "Invalid input: 'guess' must be a string."}), 400

    guess = guess.strip().lower()

    game = sessions.get(player)
    if not game:
        return jsonify({"error": "Jogo não iniciado para este jogador."}), 404
    if game['done']:
        return jsonify({"status": "info", "message": "Você já completou todas as palavras."}), 200

    idx = game['current']
    correct_word = game['words'][idx]

    if guess == correct_word:
        game['current'] += 1
        if game['current'] == 5:
            game['done'] = True
            return jsonify({
                "status": "correct",
                "message": "Parabéns! Você acertou todas as palavras."
            }), 200
        else:
            return jsonify({
                "status": "correct",
                "message": "Acertou! Próxima palavra."
            }), 200
    else:
        current_word = game['words'][idx]
        revealed = game['revealed_letters'][idx]

        if len(revealed) < len(current_word):
            game['revealed_letters'][idx] = current_word[:len(revealed)+1].upper()

        return jsonify({
            "status": "incorrect",
            "hint": game['revealed_letters'][idx],
            "message": "Errou. Letra adicionada e a vez passa."
        }), 200

@app.route("/status/<player>", methods=["GET"])
def get_status(player):
    if not player or not isinstance(player, str):
        return jsonify({"error": "Invalid input: 'player' must be a non-empty string."}), 400

    game = sessions.get(player)
    if not game:
        return jsonify({"error": "Jogador não encontrado."}), 404

    return jsonify({
        "status": "success",
        "revealed_letters": game['revealed_letters'],
        "current_word_index": game['current'],
        "game_finished": game['done']
    }), 200

@app.route("/")
def index():
    return render_template("index.html")

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000)
