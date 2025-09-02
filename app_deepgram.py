import os
import logging
from dotenv import load_dotenv
from flask import Flask, render_template, request
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from deepgram import DeepgramClient, LiveTranscriptionEvents
import google.generativeai as genai
import requests

# --- Initialization ---
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-very-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///conversations.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

logging.basicConfig(level=logging.INFO)
db = SQLAlchemy(app)
socketio = SocketIO(app, async_mode='eventlet')

# --- API Clients Initialization ---
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

deepgram_client = DeepgramClient(DEEPGRAM_API_KEY)
deepgram_connections = {}

genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = """You are an expert, friendly, and efficient hotel booking assistant for "The HighOnSwift Hotel". Your goal is to help users book a room. Be conversational and concise."""
generation_config = {"temperature": 0.7, "top_p": 1, "top_k": 1, "max_output_tokens": 2048}
gemini_model = genai.GenerativeModel('gemini-1.5-flash', generation_config=generation_config, system_instruction=SYSTEM_INSTRUCTION)

# --- Database Model (Unchanged) ---
class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(10), nullable=False)
    text = db.Column(db.String(1000), nullable=False)
    def to_dict(self): return {"role": self.role, "parts": [self.text]}

# --- Helper Functions (Unchanged) ---
def get_gemini_response(session_id, user_text):
    with app.app_context():
        history_records = Conversation.query.filter_by(session_id=session_id).order_by(Conversation.id).all()
        history = [record.to_dict() for record in history_records]
        chat = gemini_model.start_chat(history=history)
        response = chat.send_message(user_text)
        model_response_text = response.text
        user_message = Conversation(session_id=session_id, role='user', text=user_text)
        model_message = Conversation(session_id=session_id, role='model', text=model_response_text)
        db.session.add_all([user_message, model_message])
        db.session.commit()
        return model_response_text

def stream_elevenlabs_response(text, sid):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    headers = {"Accept": "audio/mpeg", "Content-Type": "application/json", "xi-api-key": ELEVENLABS_API_KEY}
    params = {'optimize_streaming_latency': 3}
    data = {"text": text, "model_id": "eleven_turbo_v2", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    try:
        with requests.post(url, headers=headers, json=data, stream=True, params=params) as response:
            if response.status_code == 200:
                for chunk in response.iter_content(chunk_size=4096):
                    if chunk: socketio.emit('audio_chunk', chunk, to=sid)
                socketio.emit('audio_stream_end', to=sid)
            else: logging.error(f"ElevenLabs API error: {response.status_code} - {response.text}")
    except Exception as e: logging.error(f"Error streaming from ElevenLabs: {e}")

# --- Flask & SocketIO Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect(*args, **kwargs):
    with app.app_context(): db.create_all()
    welcome_text = "Welcome to The HighOnSwift Hotel's booking service. How can I help you today?"
    socketio.emit('agent_response', {'text': welcome_text}, to=request.sid)
    socketio.start_background_task(stream_elevenlabs_response, welcome_text, request.sid)

@socketio.on('start_stream')
def handle_start_stream():
    sid = request.sid
    try:
        # Create live connection (Python SDK v3)
        dg_live = deepgram_client.listen.live.v("1")
        deepgram_connections[sid] = {'connection': dg_live, 'full_transcript': ''}

        def on_transcript(self, result, **kwargs):
            # Guard for Results messages
            if not hasattr(result, "channel") or not result.channel.alternatives:
                return

            transcript = result.channel.alternatives[0].transcript or ""
            if transcript == "":
                return

            # Emit interim/final text to the browser
            socketio.emit(
                'transcript_update',
                {
                    'text': transcript,
                    'is_final': bool(getattr(result, "is_final", False)),
                    'speech_final': bool(getattr(result, "speech_final", False)),
                },
                to=sid,
            )

            # Accumulate final chunks server-side
            if getattr(result, "is_final", False):
                deepgram_connections[sid]['full_transcript'] += transcript + " "

            # On end of utterance, trigger your LLM + TTS flow
            if getattr(result, "speech_final", False):
                final_text = deepgram_connections[sid]['full_transcript'].strip()
                if final_text:
                    model_response = get_gemini_response(sid, final_text)
                    socketio.emit('agent_response', {'text': model_response}, to=sid)
                    socketio.start_background_task(stream_elevenlabs_response, model_response, sid)
                deepgram_connections[sid]['full_transcript'] = ''

        def on_error(self, error, **kwargs):
            logging.error(f"Deepgram error for SID {sid}: {error}")

        dg_live.on(LiveTranscriptionEvents.Transcript, on_transcript)
        dg_live.on(LiveTranscriptionEvents.Error, on_error)

        options = {
            "model": "nova-2",
            "language": "en-US",
            "smart_format": True,
            "encoding": "linear16",
            "sample_rate": 16000,
            "endpointing": "200",
            "utterance_end_ms": "1000",
            "interim_results": True
        }
        dg_live.start(options)
        logging.info(f"Deepgram connection started successfully for SID: {sid}")

    except Exception as e:
        logging.error(f"Could not start Deepgram connection for SID {sid}: {e}")

@socketio.on('audio_chunk')
def handle_audio_chunk(chunk):
    sid = request.sid
    if sid in deepgram_connections:
        deepgram_connections[sid]['connection'].send(chunk)

@socketio.on('stop_stream')
def handle_stop_stream():
    sid = request.sid
    if sid in deepgram_connections:
        deepgram_connections[sid]['connection'].finish()

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in deepgram_connections:
        deepgram_connections[sid]['connection'].finish()
        del deepgram_connections[sid]

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5001)