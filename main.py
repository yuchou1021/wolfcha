"""FastAPI 后端 - 狼人杀游戏服务"""

import asyncio
import json
import os
import re
import time
import uuid
from typing import Dict, List, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import uvicorn

from game_controller import GameController, GameEvent


app = FastAPI(title="AgentWereWolf 狼人杀游戏")

# ========== 房间管理 ==========

class Room:
    """游戏房间"""
    def __init__(self, room_id: str, player_count: int, difficulty: str, owner_id: str):
        self.room_id = room_id
        self.player_count = player_count
        self.difficulty = difficulty
        self.owner_id = owner_id  # 房主的 player_uid
        self.seats: Dict[int, dict] = {}  # seat -> {"uid": str, "name": str}
        self.controller: Optional[GameController] = None
        self.game_started = False
        self.connections: Dict[str, WebSocket] = {}  # uid -> WebSocket
        self.ws_locks: Dict[str, asyncio.Lock] = {}  # uid -> Lock
        self.created_at = time.time()
        self.last_client_activity = self.created_at  # 最后一次有玩家操作/连接变化
        self.game_task: Optional[asyncio.Task] = None

    def touch(self):
        """玩家有动作时刷新活跃时间"""
        self.last_client_activity = time.time()

    def idle_seconds(self) -> float:
        return max(0.0, time.time() - self.last_client_activity)


    def add_player(self, seat: int, uid: str, name: str) -> bool:
        """添加玩家到座位"""
        if seat < 0 or seat >= self.player_count:
            return False
        if seat in self.seats:
            return False
        # 检查 uid 是否已经在其他座位
        for s, info in self.seats.items():
            if info["uid"] == uid:
                return False
        self.seats[seat] = {"uid": uid, "name": name}
        return True

    def remove_player(self, uid: str) -> Optional[int]:
        """移除玩家，返回座位号"""
        for seat, info in list(self.seats.items()):
            if info["uid"] == uid:
                del self.seats[seat]
                return seat
        return None

    def get_seat_by_uid(self, uid: str) -> Optional[int]:
        """根据 uid 获取座位号"""
        for seat, info in self.seats.items():
            if info["uid"] == uid:
                return seat
        return None

    def get_uid_by_seat(self, seat: int) -> Optional[str]:
        """根据座位号获取 uid"""
        info = self.seats.get(seat)
        return info["uid"] if info else None

    def get_seats_info(self) -> List[dict]:
        """获取所有座位信息"""
        seats = []
        for i in range(self.player_count):
            if i in self.seats:
                seats.append({
                    "seat": i,
                    "name": self.seats[i]["name"],
                    "occupied": True,
                    "is_owner": self.seats[i]["uid"] == self.owner_id,
                })
            else:
                seats.append({
                    "seat": i,
                    "name": "",
                    "occupied": False,
                    "is_owner": False,
                })
        return seats

    def switch_seat(self, uid: str, new_seat: int) -> bool:
        """换座位"""
        if new_seat < 0 or new_seat >= self.player_count:
            return False
        if new_seat in self.seats:
            return False
        old_seat = None
        old_name = ""
        for s, info in self.seats.items():
            if info["uid"] == uid:
                old_seat = s
                old_name = info["name"]
                break
        if old_seat is None:
            return False
        del self.seats[old_seat]
        self.seats[new_seat] = {"uid": uid, "name": old_name}
        return True

    def get_human_seats(self) -> List[int]:
        """获取所有人类玩家的座位"""
        return sorted(self.seats.keys())

    async def broadcast_room_update(self):
        """广播房间状态更新给所有连接的玩家"""
        msg = {
            "type": "room_update",
            "data": {
                "seats": self.get_seats_info(),
                "player_count": self.player_count,
                "game_started": self.game_started,
            }
        }
        for uid, ws in list(self.connections.items()):
            lock = self.ws_locks.get(uid)
            if lock:
                try:
                    async with lock:
                        await ws.send_json(msg)
                except Exception:
                    pass


# 存储活跃的房间
rooms: Dict[str, Room] = {}


class CreateRoomRequest(BaseModel):
    """创建房间请求"""
    player_count: int = 10
    difficulty: str = "normal"


class JoinRoomRequest(BaseModel):
    """加入房间请求"""
    seat: int
    name: str


@app.get("/")
async def index():
    """返回主页"""
    return FileResponse("static/index.html")


@app.get("/room/{room_id}")
async def room_page(room_id: str):
    """房间页面（和主页相同，前端根据 URL 判断）"""
    return FileResponse("static/index.html")


@app.post("/api/create_room")
async def create_room(request: CreateRoomRequest):
    """创建房间"""
    room_id = str(uuid.uuid4())[:8]
    owner_id = str(uuid.uuid4())
    room = Room(
        room_id=room_id,
        player_count=request.player_count,
        difficulty=request.difficulty,
        owner_id=owner_id,
    )
    rooms[room_id] = room

    # 创建 GameController
    controller = GameController()
    from lib.game_master import create_initial_game_state
    initial_state = create_initial_game_state()
    initial_state.game_id = room_id
    controller.state = initial_state
    room.controller = controller

    return {
        "room_id": room_id,
        "owner_id": owner_id,
        "player_count": request.player_count,
    }


@app.get("/api/room/{room_id}")
async def get_room_info(room_id: str):
    """获取房间信息"""
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    return {
        "room_id": room.room_id,
        "player_count": room.player_count,
        "difficulty": room.difficulty,
        "seats": room.get_seats_info(),
        "game_started": room.game_started,
    }


@app.websocket("/ws/{room_id}/{uid}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, uid: str):
    """WebSocket 连接 - 每个玩家一个连接"""
    await websocket.accept()

    room = rooms.get(room_id)
    if not room:
        await websocket.send_json({"type": "error", "data": {"message": "房间不存在"}})
        await websocket.close()
        return

    # 注册连接
    room.connections[uid] = websocket
    room.ws_locks[uid] = asyncio.Lock()
    room.touch()

    # 心跳任务
    heartbeat_stop = asyncio.Event()
    lock = room.ws_locks[uid]

    async def heartbeat():
        while not heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                try:
                    async with lock:
                        await websocket.send_json({"type": "ping", "data": {}})
                except Exception:
                    break

    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        # 发送连接成功
        is_owner = (uid == room.owner_id)
        await websocket.send_json({
            "type": "connected",
            "data": {
                "room_id": room_id,
                "uid": uid,
                "is_owner": is_owner,
                "seats": room.get_seats_info(),
                "player_count": room.player_count,
                "game_started": room.game_started,
            }
        })

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            room.touch()

            if msg_type == "join_seat":
                # 加入座位
                seat = data.get("seat")
                name = data.get("name", "玩家")
                if seat is not None:
                    success = room.add_player(seat, uid, name)
                    if success:
                        await websocket.send_json({
                            "type": "join_result",
                            "data": {"success": True, "seat": seat}
                        })
                        await room.broadcast_room_update()
                    else:
                        await websocket.send_json({
                            "type": "join_result",
                            "data": {"success": False, "message": "座位已被占用或你已在其他座位"}
                        })

            elif msg_type == "leave_seat":
                # 离开座位
                removed_seat = room.remove_player(uid)
                if removed_seat is not None:
                    await room.broadcast_room_update()

            elif msg_type == "switch_seat":
                # 换座位
                new_seat = data.get("seat")
                if new_seat is not None and not room.game_started:
                    success = room.switch_seat(uid, new_seat)
                    if success:
                        await websocket.send_json({
                            "type": "join_result",
                            "data": {"success": True, "seat": new_seat}
                        })
                        await room.broadcast_room_update()
                    else:
                        await websocket.send_json({
                            "type": "join_result",
                            "data": {"success": False, "message": "换座失败，目标座位已被占用"}
                        })

            elif msg_type == "start_game":
                # 房主开始游戏
                if uid != room.owner_id:
                    await websocket.send_json({
                        "type": "error",
                        "data": {"message": "只有房主可以开始游戏"}
                    })
                    continue

                if room.game_started:
                    continue

                room.game_started = True
                human_seats = room.get_human_seats()

                # 设置事件回调 - 按玩家过滤推送（含观战者）
                # 私密事件类型，观战者不可见
                PRIVATE_VISIBILITY = {"wolf_only", "guard_only", "witch_only", "seer_only"}

                async def make_event_handler(r: Room):
                    async def event_handler(event: GameEvent, target_seats: Optional[List[int]] = None):
                        """推送事件给指定座位的玩家，None 表示所有人（含观战者）"""
                        vis = event.data.get("visibility_type") or event.data.get("visibility")
                        is_private = vis in PRIVATE_VISIBILITY
                        is_targeted = target_seats is not None

                        for s_uid, ws in list(r.connections.items()):
                            seat = r.get_seat_by_uid(s_uid)
                            is_spectator = seat is None

                            if is_spectator:
                                # 观战者：跳过私密事件和定向事件（如角色揭示）
                                if is_private or is_targeted:
                                    continue
                            else:
                                # 玩家：按座位过滤
                                if is_targeted and seat not in target_seats:
                                    continue

                            ws_lock = r.ws_locks.get(s_uid)
                            if ws_lock:
                                try:
                                    async with ws_lock:
                                        await ws.send_json({
                                            "type": event.type,
                                            "data": event.data,
                                        })
                                except Exception as e:
                                    print(f"WebSocket send error to {s_uid}: {e}")
                    return event_handler

                room.controller.event_callback = await make_event_handler(room)

                # 确定第一个人类座位作为 primary human_seat（兼容 setup_players）
                primary_seat = human_seats[0] if human_seats else 0
                primary_name = room.seats[primary_seat]["name"] if primary_seat in room.seats else "玩家"

                # 广播游戏开始
                await room.broadcast_room_update()

                async def _run_game():
                    try:
                        await room.controller.start_game(
                            player_count=room.player_count,
                            human_seat=primary_seat,
                            human_name=primary_name,
                            difficulty=room.difficulty,
                            human_seats=human_seats,
                            room=room,
                        )
                    except Exception as e:
                        print(f"Game error: {e}")
                        import traceback
                        traceback.print_exc()
                        # 通知所有玩家
                        for s_uid, ws in list(room.connections.items()):
                            ws_lock = room.ws_locks.get(s_uid)
                            if ws_lock:
                                try:
                                    async with ws_lock:
                                        await ws.send_json({
                                            "type": "error",
                                            "data": {"message": f"游戏异常: {e}"}
                                        })
                                except Exception:
                                    pass

                room.game_task = asyncio.create_task(_run_game())

            elif msg_type == "action":
                # 玩家操作
                action_type = data.get("action_type")
                action_data = data.get("data")
                seat = room.get_seat_by_uid(uid)
                if seat is not None and room.controller:
                    room.controller.submit_human_action(action_type, action_data, seat)

            elif msg_type == "get_state":
                if room.controller:
                    state = room.controller.get_game_state_dict()
                    seat = room.get_seat_by_uid(uid)
                    state["my_seat"] = seat
                    # 预言家重连后要能恢复"谁查过、什么结果"
                    me = next((p for p in room.controller.state.players if p.seat == seat), None)
                    if me and me.role == "Seer":
                        state["seer_history"] = room.controller.state.night_actions.seer_history or []
                    await websocket.send_json({
                        "type": "state",
                        "data": state
                    })

            elif msg_type == "stop":
                if room.controller:
                    room.controller.stop_game()

            elif msg_type == "restart_game":
                # 重新开始 - 重置房间状态
                if uid == room.owner_id:
                    room.game_started = False
                    # 重新创建 controller
                    controller = GameController()
                    from lib.game_master import create_initial_game_state
                    initial_state = create_initial_game_state()
                    initial_state.game_id = room_id
                    controller.state = initial_state
                    room.controller = controller
                    await room.broadcast_room_update()
                    # 通知所有人回到房间
                    for s_uid, ws in list(room.connections.items()):
                        ws_lock = room.ws_locks.get(s_uid)
                        if ws_lock:
                            try:
                                async with ws_lock:
                                    await ws.send_json({
                                        "type": "back_to_room",
                                        "data": {
                                            "seats": room.get_seats_info(),
                                            "player_count": room.player_count,
                                        }
                                    })
                            except Exception:
                                pass

    except WebSocketDisconnect:
        print(f"WebSocket disconnected: {room_id}/{uid}")
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        room.touch()
        if uid in room.connections:
            del room.connections[uid]
        if uid in room.ws_locks:
            del room.ws_locks[uid]
        # 如果游戏未开始，离开座位并通知
        if not room.game_started:
            removed = room.remove_player(uid)
            if removed is not None:
                asyncio.create_task(room.broadcast_room_update())


# 挂载静态文件
static_path = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")


# ========== 总控制台（只给自己看） ==========

PHASE_LABELS = {
    "LOBBY": "大厅",
    "INIT": "准备",
    "NIGHT_START": "夜里",
    "NIGHT_DISCUSSION": "狼人商量",
    "NIGHT_GUARD_ACTION": "守卫行动",
    "NIGHT_WOLF_ACTION": "狼人行动",
    "NIGHT_WITCH_ACTION": "女巫行动",
    "NIGHT_SEER_ACTION": "预言家查验",
    "NIGHT_RESOLVE": "夜间结算",
    "DAY_START": "天亮",
    "DAY_SPEECH": "白天发言",
    "DAY_VOTE_PREP": "投票整理",
    "DAY_VOTE": "投票",
    "DAY_RESOLVE": "投票结算",
    "DAY_BADGE_SIGNUP": "警徽竞选",
    "DAY_BADGE_SPEECH": "警徽发言",
    "DAY_BADGE_ELECTION": "警徽投票",
    "HUNTER_SHOOT": "猎人开枪",
    "WHITE_WOLF_KING_BOOM": "白狼王自爆",
    "GAME_END": "已结束",
}

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def _is_private_client(host: str) -> bool:
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True
    if host.startswith("192.168.") or host.startswith("10."):
        return True
    if re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host):
        return True
    return False


def _check_admin(request: Request):
    """管理接口鉴权：默认只允许本机/局域网；设置了 ADMIN_TOKEN 就必须带口令。"""
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if token:
        provided = request.query_params.get("token") or request.headers.get("x-admin-token") or ""
        if provided != token:
            raise HTTPException(status_code=403, detail="管理口令不正确")
        return
    host = request.client.host if request.client else ""
    if not _is_private_client(host):
        raise HTTPException(status_code=403, detail="管理后台只允许局域网访问")


def _owner_name(room: Room) -> str:
    for seat, info in room.seats.items():
        if info["uid"] == room.owner_id:
            return info["name"]
    return ""


def _room_summary(room: Room) -> dict:
    state = room.controller.state if room.controller else None
    phase = getattr(state, "phase", None)
    seats = room.get_seats_info()
    seated = [s for s in seats if s["occupied"]]
    connections = len(room.connections)
    return {
        "room_id": room.room_id,
        "player_count": room.player_count,
        "difficulty": room.difficulty,
        "game_started": room.game_started,
        "day": getattr(state, "day", None),
        "phase": phase,
        "phase_label": PHASE_LABELS.get(phase, phase or ""),
        "winner": getattr(state, "winner", None),
        "seated": len(seated),
        "seats": seats,
        "owner": _owner_name(room),
        "connections": connections,
        "spectators": max(0, connections - len(seated)),
        "created_at": room.created_at,
        "last_activity_at": room.last_client_activity,
        "idle_seconds": round(room.idle_seconds(), 1),
        "has_running_task": bool(room.game_task and not room.game_task.done()),
    }


async def _notify_room(room: Room, message: dict):
    for uid, ws in list(room.connections.items()):
        lock = room.ws_locks.get(uid)
        try:
            if lock:
                async with lock:
                    await ws.send_json(message)
            else:
                await ws.send_json(message)
        except Exception:
            pass


async def close_room(room: Room, reason: str = "房间已被管理员关闭"):
    """彻底关掉一个房间：停游戏、通知玩家、断开连接、从房间表里移除。"""
    task = room.game_task
    room.game_task = None
    if task and not task.done():
        task.cancel()
    if room.controller:
        try:
            room.controller.stop_game()
        except Exception:
            pass

    await _notify_room(room, {"type": "room_closed", "data": {"message": reason}})
    for uid, ws in list(room.connections.items()):
        try:
            await ws.close()
        except Exception:
            pass
    room.connections.clear()
    room.ws_locks.clear()
    rooms.pop(room.room_id, None)


@app.get("/admin")
async def admin_page():
    """总控制台页面（页面本身不鉴权，接口鉴权）"""
    path = os.path.join(static_path, "admin.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="缺少 static/admin.html")
    return FileResponse(path)


@app.get("/api/admin/rooms")
async def admin_rooms(request: Request):
    _check_admin(request)
    items = sorted((_room_summary(room) for room in rooms.values()), key=lambda item: item["created_at"], reverse=True)
    return {
        "ok": True,
        "server_time": time.time(),
        "stats": {
            "rooms": len(items),
            "playing": sum(1 for item in items if item["game_started"]),
            "waiting": sum(1 for item in items if not item["game_started"]),
            "connections": sum(item["connections"] for item in items),
            "seated": sum(item["seated"] for item in items),
        },
        "rooms": items,
    }


@app.delete("/api/admin/rooms/{room_id}")
async def admin_delete_room(room_id: str, request: Request):
    _check_admin(request)
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    await close_room(room)
    print(f"[控制台] 已删除房间 {room_id}")
    return {"ok": True, "deleted": room_id}


@app.post("/api/admin/rooms/{room_id}/reset")
async def admin_reset_room(room_id: str, request: Request):
    """结束本局，所有人回到房间大厅（不删房间）"""
    _check_admin(request)
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")

    task = room.game_task
    room.game_task = None
    if task and not task.done():
        task.cancel()
    if room.controller:
        try:
            room.controller.stop_game()
        except Exception:
            pass

    room.game_started = False
    controller = GameController()
    from lib.game_master import create_initial_game_state
    initial_state = create_initial_game_state()
    initial_state.game_id = room.room_id
    controller.state = initial_state
    room.controller = controller
    room.touch()

    await room.broadcast_room_update()
    await _notify_room(room, {
        "type": "back_to_room",
        "data": {"seats": room.get_seats_info(), "player_count": room.player_count},
    })
    print(f"[控制台] 已结束房间 {room_id} 的本局")
    return {"ok": True, "room_id": room_id}


@app.post("/api/admin/cleanup")
async def admin_cleanup(request: Request, idle_seconds: int = 120):
    """清理没人连着的房间（默认 2 分钟没有任何玩家动作）"""
    _check_admin(request)
    removed = []
    for room in list(rooms.values()):
        if len(room.connections) == 0 and room.idle_seconds() >= max(10, idle_seconds):
            await close_room(room, "房间空闲太久，已被清理")
            removed.append(room.room_id)
    print(f"[控制台] 清理空闲房间 {len(removed)} 个: {', '.join(removed) if removed else '无'}")
    return {"ok": True, "removed": removed, "count": len(removed)}


def main():
    """启动服务器"""
    port = int(os.environ.get("PORT", "8080"))
    print("=" * 50)
    print("AgentWereWolf 狼人杀游戏服务器")
    print("=" * 50)
    print(f"访问 http://localhost:{port} 开始游戏")
    print(f"总控制台 http://127.0.0.1:{port}/admin")
    print("=" * 50)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        timeout_keep_alive=120,
        ws_ping_interval=15,
        ws_ping_timeout=120,
    )


if __name__ == "__main__":
    main()
