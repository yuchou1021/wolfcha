// 总控制台：拉房间列表、结束本局、删除房间、清理空闲房间
(function () {
    "use strict";

    const TOKEN_KEY = "wolfcha.admin.token";
    const POLL_MS = 2000;

    const state = {
        token: sessionStorage.getItem(TOKEN_KEY) || new URLSearchParams(location.search).get("token") || "",
        timer: null,
        loading: false,
        rooms: [],
    };

    const el = {
        body: document.getElementById("rooms-body"),
        stats: document.getElementById("stats"),
        auto: document.getElementById("auto-refresh"),
        refresh: document.getElementById("refresh-btn"),
        cleanup: document.getElementById("cleanup-btn"),
        toast: document.getElementById("toast"),
        tokenbar: document.getElementById("tokenbar"),
        tokenInput: document.getElementById("token-input"),
        tokenSave: document.getElementById("token-save"),
        hint: document.getElementById("server-hint"),
    };

    let toastTimer = null;
    function toast(message, tone) {
        el.toast.textContent = message;
        el.toast.className = "toast show" + (tone === "error" ? " error" : "");
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => {
            el.toast.className = "toast";
        }, 2400);
    }

    function apiUrl(path) {
        if (!state.token) return path;
        return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(state.token);
    }

    async function api(path, options) {
        const response = await fetch(apiUrl(path), options || {});
        if (response.status === 403) {
            el.tokenbar.classList.add("show");
            throw new Error("需要管理口令");
        }
        if (!response.ok) {
            let detail = "请求失败";
            try {
                const data = await response.json();
                detail = data.detail || detail;
            } catch (error) {
                /* ignore */
            }
            throw new Error(detail);
        }
        return response.json();
    }

    function fmtIdle(seconds) {
        const total = Math.max(0, Math.round(seconds || 0));
        if (total < 60) return total + " 秒";
        const minutes = Math.floor(total / 60);
        if (minutes < 60) return minutes + " 分 " + (total % 60) + " 秒";
        const hours = Math.floor(minutes / 60);
        return hours + " 时 " + (minutes % 60) + " 分";
    }

    function fmtClock(ts) {
        if (!ts) return "—";
        const date = new Date(ts * 1000);
        return date.toLocaleTimeString("zh-CN", { hour12: false });
    }

    function statusOf(room) {
        if (!room.game_started) return { cls: "waiting", text: "等待中" };
        if (room.phase === "GAME_END" || room.winner) return { cls: "ended", text: "已结束" };
        return { cls: "playing", text: "进行中" };
    }

    function renderStats(stats) {
        el.stats.innerHTML = "";
        const items = [
            ["房间", stats.rooms],
            ["进行中", stats.playing],
            ["等待中", stats.waiting],
            ["在线连接", stats.connections],
            ["已入座", stats.seated],
        ];
        for (const [label, value] of items) {
            const box = document.createElement("div");
            box.className = "stat";
            const b = document.createElement("b");
            b.textContent = String(value);
            const span = document.createElement("span");
            span.textContent = label;
            box.append(b, span);
            el.stats.append(box);
        }
    }

    function renderRooms(rooms) {
        el.body.innerHTML = "";
        if (!rooms.length) {
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.colSpan = 9;
            td.className = "empty";
            td.textContent = "现在没有房间。手机打开首页建房后会出现在这里。";
            tr.append(td);
            el.body.append(tr);
            return;
        }

        for (const room of rooms) {
            const tr = document.createElement("tr");

            const codeTd = document.createElement("td");
            const code = document.createElement("span");
            code.className = "code";
            code.textContent = room.room_id;
            codeTd.append(code);

            const statusTd = document.createElement("td");
            const status = statusOf(room);
            const badge = document.createElement("span");
            badge.className = "badge " + status.cls;
            badge.textContent = status.text;
            statusTd.append(badge);

            const modeTd = document.createElement("td");
            modeTd.className = "hide-sm muted";
            modeTd.textContent = room.player_count + " 人 · " + (room.difficulty === "hard" ? "困难" : room.difficulty === "easy" ? "简单" : "普通");

            const progressTd = document.createElement("td");
            if (room.game_started) {
                progressTd.textContent = "第 " + (room.day || "?") + " 天 · " + (room.phase_label || room.phase || "");
            } else {
                progressTd.className = "muted";
                progressTd.textContent = room.owner ? "房主 " + room.owner : "—";
            }

            const namesTd = document.createElement("td");
            namesTd.className = "hide-sm names";
            const seated = room.seats.filter((seat) => seat.occupied);
            if (!seated.length) {
                namesTd.className = "hide-sm muted";
                namesTd.textContent = "—";
            } else {
                for (const seat of seated) {
                    const chip = document.createElement("span");
                    chip.className = "chip";
                    chip.textContent = (seat.seat + 1) + "号 " + seat.name + (seat.is_owner ? " ♛" : "");
                    namesTd.append(chip);
                }
            }

            const connTd = document.createElement("td");
            connTd.className = "mono";
            connTd.textContent = String(room.connections);
            if (room.spectators > 0) {
                const sup = document.createElement("span");
                sup.className = "muted";
                sup.textContent = " (+" + room.spectators + " 观战)";
                connTd.append(sup);
            }

            const idleTd = document.createElement("td");
            idleTd.className = "mono muted";
            idleTd.textContent = fmtIdle(room.idle_seconds);

            const createdTd = document.createElement("td");
            createdTd.className = "hide-sm mono muted";
            createdTd.textContent = fmtClock(room.created_at);

            const actionTd = document.createElement("td");
            const actions = document.createElement("div");
            actions.className = "row-actions";

            if (room.game_started) {
                const resetBtn = document.createElement("button");
                resetBtn.type = "button";
                resetBtn.className = "small";
                resetBtn.textContent = "结束本局";
                resetBtn.addEventListener("click", () => resetRoom(room));
                actions.append(resetBtn);
            }

            const copyBtn = document.createElement("button");
            copyBtn.type = "button";
            copyBtn.className = "small";
            copyBtn.textContent = "复制链接";
            copyBtn.addEventListener("click", () => copyLink(room));
            actions.append(copyBtn);

            const delBtn = document.createElement("button");
            delBtn.type = "button";
            delBtn.className = "small danger";
            delBtn.textContent = "删除";
            delBtn.addEventListener("click", () => deleteRoom(room));
            actions.append(delBtn);

            actionTd.append(actions);

            tr.append(codeTd, statusTd, modeTd, progressTd, namesTd, connTd, idleTd, createdTd, actionTd);
            el.body.append(tr);
        }
    }

    async function load() {
        if (state.loading) return;
        state.loading = true;
        try {
            const data = await api("/api/admin/rooms");
            state.rooms = data.rooms;
            renderStats(data.stats);
            renderRooms(data.rooms);
            el.tokenbar.classList.remove("show");
            el.hint.textContent = "共 " + data.stats.rooms + " 个房间 · 服务时间 " + fmtClock(data.server_time);
        } catch (error) {
            el.hint.textContent = error.message;
            if (error.message !== "需要管理口令") toast(error.message, "error");
        } finally {
            state.loading = false;
        }
    }

    async function deleteRoom(room) {
        const seated = room.seats.filter((seat) => seat.occupied).length;
        const message = room.game_started
            ? "房间 " + room.room_id + " 正在游戏中，删除会把这局直接结束并踢掉所有人。确定？"
            : "删除房间 " + room.room_id + "（" + seated + " 位玩家在座）？";
        if (!window.confirm(message)) return;
        try {
            await api("/api/admin/rooms/" + encodeURIComponent(room.room_id), { method: "DELETE" });
            toast("已删除房间 " + room.room_id);
            await load();
        } catch (error) {
            toast(error.message, "error");
        }
    }

    async function resetRoom(room) {
        if (!window.confirm("结束房间 " + room.room_id + " 的这一局，所有人回到大厅？（房间和座位保留）")) return;
        try {
            await api("/api/admin/rooms/" + encodeURIComponent(room.room_id) + "/reset", { method: "POST" });
            toast("已结束这一局，玩家回到大厅");
            await load();
        } catch (error) {
            toast(error.message, "error");
        }
    }

    async function copyLink(room) {
        const url = location.origin + "/room/" + room.room_id;
        try {
            await navigator.clipboard.writeText(url);
            toast("已复制：" + url);
        } catch (error) {
            window.prompt("手动复制房间链接：", url);
        }
    }

    async function cleanup() {
        if (!window.confirm("删除所有「没有任何连接」且空闲超过 2 分钟的房间？正在玩的房间不受影响。")) return;
        try {
            const data = await api("/api/admin/cleanup?idle_seconds=120", { method: "POST" });
            toast(data.count ? "清理了 " + data.count + " 个空闲房间" : "没有需要清理的房间");
            await load();
        } catch (error) {
            toast(error.message, "error");
        }
    }

    function startPolling() {
        stopPolling();
        if (!el.auto.checked) return;
        state.timer = setInterval(() => {
            if (document.visibilityState === "visible") load();
        }, POLL_MS);
    }

    function stopPolling() {
        if (state.timer) clearInterval(state.timer);
        state.timer = null;
    }

    el.refresh.addEventListener("click", load);
    el.cleanup.addEventListener("click", cleanup);
    el.auto.addEventListener("change", startPolling);
    el.tokenSave.addEventListener("click", () => {
        state.token = el.tokenInput.value.trim();
        sessionStorage.setItem(TOKEN_KEY, state.token);
        el.tokenbar.classList.remove("show");
        load();
    });
    el.tokenInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") el.tokenSave.click();
    });
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") load();
    });

    load();
    startPolling();
})();
