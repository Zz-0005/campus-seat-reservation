/* 校园自习座位预约系统 —— 前端逻辑（原生 JS，无框架） */
(() => {
  'use strict';

  const STORAGE_KEY = 'campus-seat-user';
  const POLL_MS = 5000;
  const KIND_LABEL = { window: '靠窗', power: '有电源', normal: '' };
  const STATUS_LABEL = {
    upcoming: '未开始',
    ongoing: '进行中',
    finished: '已结束',
    cancelled: '已取消',
  };

  const state = {
    user: null,
    meta: null,
    roomId: null,
    date: null,
    room: null,
    seats: [],
    slots: [],
    selSeatId: null,
    selStart: null,
    selEnd: null,
    reservations: [],
    pollTimer: null,
  };

  const $ = (id) => document.getElementById(id);

  // ---------- 与后端通信 ----------
  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    let data = null;
    const text = await res.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (err) {
        data = null;
      }
    }
    if (!res.ok) {
      const error = new Error((data && data.error) || `请求失败（${res.status}）`);
      error.status = res.status;
      error.code = data && data.code;
      error.payload = data;
      throw error;
    }
    return data;
  }

  // ---------- 提示 ----------
  let toastTimer = null;
  function toast(message, type = 'ok') {
    const el = $('toast');
    el.textContent = message;
    el.className = `toast show ${type}`;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.className = 'toast';
      el.hidden = true;
    }, 2600);
  }

  // ---------- 身份 ----------
  function loadUser() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (parsed && parsed.id && parsed.name) return parsed;
    } catch (err) {
      /* 忽略损坏的本地数据 */
    }
    return null;
  }

  function saveUser(user) {
    state.user = user;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(user));
    renderIdentity();
  }

  function renderIdentity() {
    if (state.user) {
      $('userNameLabel').textContent = state.user.name;
      $('userIdLabel').textContent = `学号 ${state.user.id}`;
      $('btnSwitchUser').textContent = '切换身份';
    } else {
      $('userNameLabel').textContent = '未设置身份';
      $('userIdLabel').textContent = '请先填写学号';
      $('btnSwitchUser').textContent = '填写身份';
    }
  }

  function openUserModal() {
    $('userError').hidden = true;
    $('inputUserId').value = state.user ? state.user.id : '';
    $('inputUserName').value = state.user ? state.user.name : '';
    $('userModal').hidden = false;
    ($('inputUserId').value ? $('inputUserName') : $('inputUserId')).focus();
  }

  function submitUser() {
    const id = $('inputUserId').value.trim();
    const name = $('inputUserName').value.trim();
    const err = $('userError');
    if (!/^[A-Za-z0-9_-]{3,20}$/.test(id)) {
      err.textContent = '学号需为 3-20 位字母、数字、下划线或连字符';
      err.hidden = false;
      return;
    }
    if (!name) {
      err.textContent = '请填写姓名';
      err.hidden = false;
      return;
    }
    saveUser({ id, name });
    $('userModal').hidden = true;
    refreshAll();
  }

  // ---------- 渲染：自习室与日期 ----------
  function renderRooms() {
    const wrap = $('roomTabs');
    wrap.innerHTML = '';
    (state.meta.rooms || []).forEach((room) => {
      const btn = document.createElement('button');
      btn.className = 'room-tab' + (room.id === state.roomId ? ' active' : '');
      btn.innerHTML = `${escapeHtml(room.name)}<small>${room.seatCount} 座</small>`;
      btn.addEventListener('click', () => {
        if (state.roomId === room.id) return;
        state.roomId = room.id;
        clearSelection();
        renderRooms();
        loadRoom();
      });
      wrap.appendChild(btn);
    });
  }

  function renderDates() {
    const wrap = $('dateChips');
    wrap.innerHTML = '';
    (state.meta.dates || []).forEach((day) => {
      const btn = document.createElement('button');
      btn.className = 'date-chip' + (day.date === state.date ? ' active' : '');
      btn.innerHTML = `${escapeHtml(day.label)}<span>${day.weekday}${day.isToday ? ' · 今天' : ''}</span>`;
      btn.addEventListener('click', () => {
        if (state.date === day.date) return;
        state.date = day.date;
        clearSelection();
        renderDates();
        loadRoom();
      });
      wrap.appendChild(btn);
    });
  }

  // ---------- 渲染：座位图 ----------
  function renderSeats() {
    const map = $('seatMap');
    const room = state.room;
    if (!room) return;
    map.style.gridTemplateColumns = `repeat(${room.room.cols}, minmax(64px, 92px))`;
    map.innerHTML = '';

    state.seats.forEach((seat) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `seat ${seat.state}`;
      if (seat.mine.length) btn.classList.add('has-mine');
      if (seat.id === state.selSeatId) btn.classList.add('selected');
      const kind = KIND_LABEL[seat.kind] || '';
      btn.innerHTML = `<span class="seat-label">${escapeHtml(seat.label)}</span>`
        + `<span class="seat-kind">${kind}</span>`;
      if (seat.mine.length) {
        const flag = document.createElement('span');
        flag.className = 'mine-flag';
        flag.textContent = '我';
        btn.appendChild(flag);
      }
      const clickable = seat.state === 'free' || seat.state === 'partial' || seat.mine.length > 0;
      btn.disabled = !clickable;
      btn.title = seatTitle(seat);
      if (clickable) {
        btn.addEventListener('click', () => selectSeat(seat));
      }
      map.appendChild(btn);
    });

    $('roomTitle').textContent = room.room.name;
    $('roomMeta').textContent = `${room.room.building} ${room.room.floor} 层 · ${room.room.description} · `
      + `${state.seats.length} 个座位`;
    const freeSeats = state.seats.filter((s) => s.state === 'free').length;
    $('seatHint').textContent = `当前 ${state.date}：全天空闲 ${freeSeats} 个，`
      + `${state.seats.filter((s) => s.state === 'partial').length} 个部分可约，`
      + `${state.seats.filter((s) => s.state === 'full').length} 个已约满。每 5 秒自动刷新。`;
  }

  function seatTitle(seat) {
    if (seat.state === 'closed') return '当天已无可约时段';
    if (seat.state === 'full') return '该座位当天剩余时段已全部预约';
    return `空闲时段 ${seat.freeCount} 个`;
  }

  // ---------- 座位与时段选择 ----------
  function selectSeat(seat) {
    if (state.selSeatId !== seat.id) {
      state.selSeatId = seat.id;
      state.selStart = null;
      state.selEnd = null;
    }
    renderSeats();
    renderBookingPanel();
  }

  function clearSelection() {
    state.selSeatId = null;
    state.selStart = null;
    state.selEnd = null;
  }

  function selectedSeat() {
    return state.seats.find((s) => s.id === state.selSeatId) || null;
  }

  function slotInfo(index) {
    return state.slots[index] || { index, start: '', end: '', bookable: false };
  }

  function slotAvailability(seat, index) {
    if (!seat) return 'free';
    if (seat.mine.includes(index)) return 'mine';
    if (seat.booked.includes(index)) return 'booked';
    if (!slotInfo(index).bookable) return 'past';
    return 'free';
  }

  function freeSlotIndexes(seat) {
    const list = [];
    for (let i = 0; i < state.slots.length; i += 1) {
      if (slotAvailability(seat, i) === 'free') list.push(i);
    }
    return list;
  }

  function rangeFree(seat, start, end) {
    for (let i = start; i < end; i += 1) {
      if (slotAvailability(seat, i) !== 'free') return false;
    }
    return true;
  }

  function onSlotClick(index) {
    const seat = selectedSeat();
    if (!seat || slotAvailability(seat, index) !== 'free') return;

    const start = state.selStart;
    const end = state.selEnd;

    if (start === null || end === null || index < start || index >= end) {
      // 首次点击、点在区间之前、点在区间之后：尝试把区间扩大到包含该时段
      if (start !== null && end !== null && index < start && rangeFree(seat, index, end)) {
        state.selStart = index;
      } else if (start !== null && end !== null && index >= end && rangeFree(seat, start, index + 1)) {
        state.selEnd = index + 1;
      } else {
        state.selStart = index;
        state.selEnd = index + 1;
      }
    } else if (index === start) {
      // 再点起点就是取消这个区间
      state.selStart = null;
      state.selEnd = null;
    } else {
      // 点在区间内部：收缩到该时段
      state.selEnd = index + 1;
    }
    renderBookingPanel();
  }

  function setQuickPick(hours) {
    const seat = selectedSeat();
    if (!seat) return;
    const free = freeSlotIndexes(seat);
    if (!free.length) return;
    let start = free[0];
    if (state.selStart !== null && free.includes(state.selStart)) start = state.selStart;
    let end = start;
    for (let i = 0; i < hours; i += 1) {
      if (slotAvailability(seat, start + i) !== 'free') break;
      end = start + i + 1;
    }
    state.selStart = start;
    state.selEnd = end;
    renderBookingPanel();
  }

  // ---------- 渲染：预约面板 ----------
  function renderBookingPanel() {
    const body = $('bookingBody');
    const title = $('bookingTitle');
    const seat = selectedSeat();

    if (!seat) {
      title.textContent = '选择一个座位';
      body.innerHTML = '<p class="muted">在左侧座位图中点一个空闲座位，这里会显示它当天的可约时段。</p>';
      return;
    }

    title.textContent = `座位 ${seat.label}`;
    const start = state.selStart;
    const end = state.selEnd;
    const hasRange = start !== null && end !== null && end > start;
    const rangeText = hasRange
      ? `${slotInfo(start).start} - ${slotInfo(end - 1).end}（${end - start} 小时）`
      : '尚未选择时段';

    const summary = document.createElement('div');
    summary.className = 'seat-summary';
    summary.innerHTML = `<span>已选时段：<strong>${rangeText}</strong></span>`
      + `<span class="slot-tag">空闲 ${seat.freeCount} 个时段</span>`;
    body.innerHTML = '';
    body.appendChild(summary);

    const picks = document.createElement('div');
    picks.className = 'quick-picks';
    [
      { label: '1 小时', hours: 1 },
      { label: '2 小时', hours: 2 },
      { label: '4 小时', hours: 4 },
    ].forEach((item) => {
      const b = document.createElement('button');
      b.className = 'btn btn-sm';
      b.textContent = item.label;
      b.addEventListener('click', () => setQuickPick(item.hours));
      picks.appendChild(b);
    });
    const clearBtn = document.createElement('button');
    clearBtn.className = 'btn btn-sm btn-ghost';
    clearBtn.textContent = '清空选择';
    clearBtn.addEventListener('click', () => {
      state.selStart = null;
      state.selEnd = null;
      renderBookingPanel();
    });
    picks.appendChild(clearBtn);
    body.appendChild(picks);

    const list = document.createElement('div');
    list.className = 'slot-list';
    state.slots.forEach((slot) => {
      const avail = slotAvailability(seat, slot.index);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'slot';
      const inRange = hasRange && slot.index >= start && slot.index < end;
      if (inRange) btn.classList.add('in-range');
      if (avail !== 'free') btn.classList.add(avail);
      btn.disabled = avail !== 'free';
      const tag = avail === 'free'
        ? (inRange ? '已选' : '可预约')
        : avail === 'booked'
          ? '已被预约'
          : avail === 'mine'
            ? '我的预约'
            : '已过时段';
      btn.innerHTML = `<span>${slot.start} - ${slot.end}</span><span class="slot-tag">${tag}</span>`;
      if (avail === 'free') btn.addEventListener('click', () => onSlotClick(slot.index));
      list.appendChild(btn);
    });
    body.appendChild(list);

    const actions = document.createElement('div');
    actions.className = 'booking-actions';
    const bookBtn = document.createElement('button');
    bookBtn.className = 'btn btn-primary';
    bookBtn.textContent = '确认预约';
    bookBtn.disabled = !hasRange;
    bookBtn.addEventListener('click', book);
    actions.appendChild(bookBtn);
    const note = document.createElement('span');
    note.className = 'booking-note';
    note.textContent = hasRange ? '提交后该时段立即锁定' : '先在上面的时段表中选一个区间';
    actions.appendChild(note);
    body.appendChild(actions);
  }

  // ---------- 渲染：我的预约 ----------
  function renderReservations() {
    const list = $('myList');
    const items = state.reservations || [];
    $('myCount').textContent = String(items.filter((r) => r.status === 'upcoming' || r.status === 'ongoing').length);
    list.innerHTML = '';
    if (!items.length) {
      list.innerHTML = '<p class="muted">还没有预约，选一个座位开始吧。</p>';
      return;
    }

    items.forEach((item) => {
      const box = document.createElement('div');
      box.className = `res-item status-${item.status}`;

      const top = document.createElement('div');
      top.className = 'res-top';
      top.innerHTML = `<span class="res-seat">${escapeHtml(item.seatLabel)} 座</span>`
        + `<span class="status ${item.status}">${STATUS_LABEL[item.status] || item.status}</span>`;
      box.appendChild(top);

      const room = document.createElement('div');
      room.className = 'res-room';
      room.textContent = `${item.building} · ${item.roomName}`;
      box.appendChild(room);

      const time = document.createElement('div');
      time.className = 'res-time';
      time.textContent = `${formatDate(item.date)} ${item.timeRange}`;
      box.appendChild(time);

      if (item.cancellable) {
        const foot = document.createElement('div');
        foot.className = 'res-foot';
        const spacer = document.createElement('span');
        spacer.className = 'res-room';
        spacer.textContent = '开始前可取消';
        foot.appendChild(spacer);
        const btn = document.createElement('button');
        btn.className = 'btn btn-danger btn-sm';
        btn.textContent = '取消预约';
        btn.addEventListener('click', () => cancelReservation(item));
        foot.appendChild(btn);
        box.appendChild(foot);
      }
      list.appendChild(box);
    });
  }

  function formatDate(iso) {
    const [y, m, d] = iso.split('-');
    return `${Number(m)} 月 ${Number(d)} 日`;
  }

  // ---------- 数据加载 ----------
  async function loadMeta() {
    state.meta = await api('/api/meta');
    if (!state.roomId && state.meta.rooms.length) state.roomId = state.meta.rooms[0].id;
    if (!state.date) state.date = state.meta.today;
    renderRooms();
    renderDates();
  }

  async function loadRoom() {
    if (!state.roomId || !state.date) return;
    const query = new URLSearchParams({ date: state.date });
    if (state.user) query.set('userId', state.user.id);
    try {
      const data = await api(`/api/rooms/${state.roomId}/seats?${query.toString()}`);
      state.room = data;
      state.seats = data.seats;
      state.slots = data.slots;

      const seat = selectedSeat();
      if (!seat) {
        clearSelection();
      } else {
        // 刷新后如果原来选的时段被别人抢走，就重置选择
        if (state.selStart !== null && !rangeFree(seat, state.selStart, state.selEnd)) {
          state.selStart = null;
          state.selEnd = null;
        }
      }
      renderSeats();
      renderBookingPanel();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async function loadReservations() {
    if (!state.user) {
      state.reservations = [];
      renderReservations();
      return;
    }
    try {
      const data = await api(`/api/reservations?userId=${encodeURIComponent(state.user.id)}`);
      state.reservations = data.reservations;
      renderReservations();
    } catch (err) {
      toast(err.message, 'error');
    }
  }

  async function refreshAll() {
    await Promise.all([loadRoom(), loadReservations()]);
  }

  // ---------- 预约与取消 ----------
  async function book() {
    const seat = selectedSeat();
    if (!seat || !state.user) return;
    if (state.selStart === null || state.selEnd === null) return;

    try {
      await api('/api/reservations', {
        method: 'POST',
        body: JSON.stringify({
          userId: state.user.id,
          userName: state.user.name,
          seatId: seat.id,
          date: state.date,
          startSlot: state.selStart,
          endSlot: state.selEnd,
        }),
      });
      toast(`预约成功：${seat.label} ${slotInfo(state.selStart).start} - ${slotInfo(state.selEnd - 1).end}`, 'ok');
      state.selStart = null;
      state.selEnd = null;
      await refreshAll();
    } catch (err) {
      toast(err.message, 'error');
      state.selStart = null;
      state.selEnd = null;
      await refreshAll();
    }
  }

  async function cancelReservation(item) {
    if (!state.user) return;
    const ok = window.confirm(`确定取消 ${item.seatLabel} 座 ${formatDate(item.date)} ${item.timeRange} 的预约吗？`);
    if (!ok) return;
    try {
      await api(`/api/reservations/${item.id}/cancel`, {
        method: 'POST',
        body: JSON.stringify({ userId: state.user.id }),
      });
      toast('已取消预约，座位已释放', 'ok');
      await refreshAll();
    } catch (err) {
      toast(err.message, 'error');
      await refreshAll();
    }
  }

  // ---------- 工具 ----------
  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  // ---------- 启动 ----------
  async function boot() {
    state.user = loadUser();
    renderIdentity();
    try {
      await loadMeta();
    } catch (err) {
      toast(`无法连接服务器：${err.message}`, 'error');
      return;
    }
    await refreshAll();
    if (!state.user) openUserModal();

    $('btnSwitchUser').addEventListener('click', openUserModal);
    $('btnSaveUser').addEventListener('click', submitUser);
    $('inputUserName').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitUser();
    });

    state.pollTimer = setInterval(refreshAll, POLL_MS);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refreshAll();
    });
  }

  document.addEventListener('DOMContentLoaded', boot);
})();
