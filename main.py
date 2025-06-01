from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

sessions = {}

@app.route("/start", methods=["POST"])
def start_game():
    """Starts a new word guessing game for a player."""
    data = request.json
    player = data.get("player")
    word_list = data.get("words")

    # Validate input
    if not player or not isinstance(player, str):
        return jsonify({"error": "Invalid input: 'player' must be a non-empty string."}), 400
    if not isinstance(word_list, list) or len(word_list) != 6:
        return jsonify({"error": "Invalid input: 'words' must be a list of exactly 6 words."}), 400
    if not all(isinstance(word, str) and word for word in word_list):
         return jsonify({"error": "Invalid input: 'words' list must contain only non-empty strings."}), 400


    # Initialize game session
    sessions[player] = {
        "words": [word.lower() for word in word_list[1:]], # Store words in lowercase
        "hint_letters": [word[0].upper() for word in word_list[1:]],
        "revealed_letters": [word[0].upper() for word in word_list[1:]],
        "current": 0,
        "done": False
    }

    return jsonify({
        "status": "success",
        "message": f"Jogo iniciado para {player}.",
        "first_word": word_list[0], # Still return the first word as is
        "hint_letters": sessions[player]['hint_letters']
    }), 201 # Use 201 Created for successful resource creation

@app.route("/guess", methods=["POST"])
def guess_word():
    """Handles a player's guess for the current word."""
    data = request.json
    player = data.get("player")
    guess = data.get("guess")

    # Validate input
    if not player or not isinstance(player, str):
        return jsonify({"error": "Invalid input: 'player' must be a non-empty string."}), 400
    if not isinstance(guess, str):
         return jsonify({"error": "Invalid input: 'guess' must be a string."}), 400

    guess = guess.strip().lower() # Process guess

    # Check if game exists and is not done
    game = sessions.get(player)
    if not game:
        return jsonify({"error": "Jogo não iniciado para este jogador."}), 404 # Use 404 Not Found
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

        # Reveal one more letter if not all letters are revealed
        if len(revealed) < len(current_word):
            game['revealed_letters'][idx] = current_word[:len(revealed)+1].upper() # Capitalize revealed letters

        return jsonify({
            "status": "incorrect",
            "hint": game['revealed_letters'][idx],
            "message": "Errou. Letra adicionada e a vez passa."
        }), 200 # Still 200 OK for an incorrect guess response

@app.route("/status/<player>", methods=["GET"])
def get_status(player):
    """Gets the current status of a player's game."""
    if not player or not isinstance(player, str):
        return jsonify({"error": "Invalid input: 'player' must be a non-empty string."}), 400

    game = sessions.get(player)
    if not game:
        return jsonify({"error": "Jogador não encontrado."}), 404

    return jsonify({
        "status": "success",
        "revealed_letters": game['revealed_letters'],
        "current_word_index": game['current'], # More descriptive key
        "game_finished": game['done']          # More descriptive key
    }), 200

from flask import render_template

@app.route("/")
def index():
    return render_template("index.html")
