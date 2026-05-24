from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime
import os
import uuid

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")

# Use threading to avoid requiring eventlet/gevent explicitly
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# In-memory room store. For demo/single-process use only.
rooms = {}  # room_id -> {"users": {sid: {"username": str}}, "messages": list, "video": {"active": bool}}


def generate_room_id() -> str:
    """Return a short 6-character uppercase room id."""
    return uuid.uuid4().hex[:6].upper()


def get_timestamp() -> str:
    return datetime.now().strftime("%H:%M")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/chat")
def chat():
    room_id = request.args.get("room")
    username = request.args.get("username")

    if not room_id or not username:
        return redirect(url_for("index"))

    if room_id not in rooms:
        # Room might have been cleaned up – go home.
        return redirect(url_for("index"))

    return render_template("chat.html", room_id=room_id, username=username)


# =========================
# Socket.IO event handlers
# =========================


@socketio.on("create_room")
def handle_create_room(data):
    username = (data or {}).get("username", "").strip()
    if not username:
        emit("error", {"message": "Username is required."})
        return

    room_id = generate_room_id()
    while room_id in rooms:
        room_id = generate_room_id()

    rooms[room_id] = {
        "users": {},
        "messages": [],
        "video": {"active": False},
    }

    emit("room_created", {"room_id": room_id, "username": username})


@socketio.on("join_room")
def handle_join_room(data):
    room_id = (data or {}).get("room_id", "").upper()
    username = (data or {}).get("username", "").strip()

    if not room_id or not username:
        emit("error", {"message": "Room ID and username are required."})
        return

    if room_id not in rooms:
        emit("error", {"message": "Room does not exist."})
        return

    room = rooms[room_id]

    # Prevent duplicate usernames inside the same room
    if any(u["username"].lower() == username.lower() for u in room["users"].values()):
        emit("error", {"message": "That username is already taken in this room."})
        return

    join_room(room_id)

    room["users"][request.sid] = {"username": username}

    # Notify this user that join succeeded
    emit(
        "room_joined",
        {
            "room_id": room_id,
            "username": username,
            "socket_id": request.sid,
            "video_active": room["video"]["active"],
        },
    )

    # Send existing users + message history to the newly joined user
    emit(
        "user_list",
        {
            "users": [
                {"socket_id": sid, "username": u["username"]}
                for sid, u in room["users"].items()
            ]
        },
    )

    emit("message_history", {"messages": room["messages"]})

    # Broadcast system message + updated user list to the rest of the room
    system_msg = {
        "type": "system",
        "username": "System",
        "message": f"{username} joined the room.",
        "timestamp": get_timestamp(),
    }
    room["messages"].append(system_msg)

    emit("new_message", system_msg, room=room_id, include_self=False)

    emit(
        "user_list",
        {
            "users": [
                {"socket_id": sid, "username": u["username"]}
                for sid, u in room["users"].items()
            ]
        },
        room=room_id,
    )


@socketio.on("send_message")
def handle_send_message(data):
    room_id = (data or {}).get("room_id")
    text = (data or {}).get("message", "").strip()

    if not room_id or room_id not in rooms:
        emit("error", {"message": "Invalid room."})
        return

    if not text:
        return

    room = rooms[room_id]
    user = room["users"].get(request.sid)
    if not user:
        emit("error", {"message": "You are not in this room."})
        return

    msg = {
        "type": "user",
        "username": user["username"],
        "message": text,
        "timestamp": get_timestamp(),
    }
    room["messages"].append(msg)
    emit("new_message", msg, room=room_id)


@socketio.on("start_video_call")
def handle_start_video_call(data):
    room_id = (data or {}).get("room_id")
    if not room_id or room_id not in rooms:
        emit("error", {"message": "Invalid room for video call."})
        return

    room = rooms[room_id]
    room["video"]["active"] = True
    caller = room["users"].get(request.sid, {}).get("username", "Someone")

    emit(
        "video_call_started",
        {"room_id": room_id, "caller": caller, "caller_id": request.sid},
        room=room_id,
    )


@socketio.on("end_video_call")
def handle_end_video_call(data):
    room_id = (data or {}).get("room_id")
    if not room_id or room_id not in rooms:
        return

    room = rooms[room_id]
    room["video"]["active"] = False

    emit("video_call_ended", {"room_id": room_id}, room=room_id)


# -------------
# WebRTC signaling (offer/answer/ICE)
# -------------


@socketio.on("webrtc_offer")
def handle_webrtc_offer(data):
    room_id = (data or {}).get("room_id")
    target_id = (data or {}).get("target_id")
    sdp = (data or {}).get("sdp")

    if not room_id or room_id not in rooms:
        emit("error", {"message": "Invalid room for offer."})
        return

    if not target_id or not sdp:
        return

    emit(
        "webrtc_offer",
        {"sdp": sdp, "sender_id": request.sid},
        to=target_id,
    )


@socketio.on("webrtc_answer")
def handle_webrtc_answer(data):
    room_id = (data or {}).get("room_id")
    target_id = (data or {}).get("target_id")
    sdp = (data or {}).get("sdp")

    if not room_id or room_id not in rooms:
        emit("error", {"message": "Invalid room for answer."})
        return

    if not target_id or not sdp:
        return

    emit(
        "webrtc_answer",
        {"sdp": sdp, "sender_id": request.sid},
        to=target_id,
    )


@socketio.on("webrtc_ice_candidate")
def handle_webrtc_ice_candidate(data):
    room_id = (data or {}).get("room_id")
    target_id = (data or {}).get("target_id")
    candidate = (data or {}).get("candidate")

    if not room_id or room_id not in rooms:
        return

    if not target_id or not candidate:
        return

    emit(
        "webrtc_ice_candidate",
        {"candidate": candidate, "sender_id": request.sid},
        to=target_id,
    )


@socketio.on("disconnect")
def handle_disconnect():
    # Find which room(s) this socket was in
    to_delete_rooms = []
    for room_id, room in list(rooms.items()):
        if request.sid in room["users"]:
            username = room["users"][request.sid]["username"]
            leave_room(room_id)
            del room["users"][request.sid]

            system_msg = {
                "type": "system",
                "username": "System",
                "message": f"{username} left the room.",
                "timestamp": get_timestamp(),
            }
            room["messages"].append(system_msg)

            emit("new_message", system_msg, room=room_id)

            emit(
                "user_list",
                {
                    "users": [
                        {"socket_id": sid, "username": u["username"]}
                        for sid, u in room["users"].items()
                    ]
                },
                room=room_id,
            )

            # If room is empty, mark for deletion
            if not room["users"]:
                to_delete_rooms.append(room_id)

    for rid in to_delete_rooms:
        rooms.pop(rid, None)


if __name__ == "__main__":
    print("Starting Flask-SocketIO chat server with video calls...")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
