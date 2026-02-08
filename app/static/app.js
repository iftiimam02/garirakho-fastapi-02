console.log("✅ Garirakho booking app.js loaded");

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function getJson(url) {
  const r = await fetch(url, { method: "GET", credentials: "include", cache: "no-store" });
  const t = await r.text();
  if (!r.ok) throw new Error(`${url} -> ${r.status} | ${t}`);
  return JSON.parse(t);
}

async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Accept": "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const t = await r.text();
  if (!r.ok) throw new Error(`POST ${url} -> ${r.status} | ${t}`);
  try { return JSON.parse(t); } catch { return {}; }
}

function toastError(err) {
  alert(String(err?.message || err));
}

window.__cmdOpenGate = () => postJson("/api/cmd/open-gate", {}).catch(toastError);
window.__cmdExitApproved = (approved) => postJson("/api/cmd/exit-approved", { approved: !!approved }).catch(toastError);

window.__adminApproveUser = (userId) => postJson("/api/admin/users/approve", { userId }).then(refreshAll).catch(toastError);
window.__adminRejectUser = (userId) => postJson("/api/admin/users/reject", { userId }).then(refreshAll).catch(toastError);

window.__requestBooking = (slotId) => postJson("/api/bookings/request", { slotId }).then(refreshAll).catch(toastError);
window.__cancelBooking = (bookingId) => postJson("/api/bookings/cancel", { bookingId }).then(refreshAll).catch(toastError);

window.__adminApproveBooking = (bookingId) => postJson("/api/admin/bookings/approve", { bookingId }).then(refreshAll).catch(toastError);
window.__adminRejectBooking = (bookingId) => postJson("/api/admin/bookings/reject", { bookingId }).then(refreshAll).catch(toastError);

function slotCard(s) {
  const state = s.state;
  const stateClass =
    state === "free" ? "bg-emerald-500/15 border-emerald-500/30 text-emerald-100" :
    state === "booked" ? "bg-amber-500/15 border-amber-500/30 text-amber-100" :
    "bg-rose-500/15 border-rose-500/30 text-rose-100";

  const btn = (state === "free")
    ? `<button class="mt-2 w-full px-3 py-2 rounded-xl bg-sky-600 hover:bg-sky-500 text-sm"
         onclick="window.__requestBooking(${s.id})">Request Booking</button>`
    : `<div class="mt-2 text-xs text-slate-400">Not available</div>`;

  return `
    <div class="border rounded-2xl p-3 ${stateClass}">
      <div class="font-semibold">Slot ${escapeHtml(s.id)}</div>
      <div class="text-sm mt-1">
        State: <span class="font-semibold">${escapeHtml(state.toUpperCase())}</span>
      </div>
      <div class="text-xs mt-1 text-slate-200/80">
        occupied=${s.occupied ? "true" : "false"} | booked=${s.booked ? "true" : "false"}
      </div>
      ${btn}
    </div>
  `;
}

function bookingRow(b) {
  const canCancel = (b.status === "pending" || b.status === "approved");
  const cancelBtn = canCancel
    ? `<button class="px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-sm border border-slate-700"
         onclick="window.__cancelBooking(${b.id})">Cancel</button>`
    : "";

  return `
    <div class="bg-slate-950/40 border border-slate-800 rounded-2xl p-3">
      <div class="flex items-start justify-between gap-3">
        <div>
          <div class="font-semibold">Booking #${escapeHtml(b.id)} (Slot ${escapeHtml(b.slotId)})</div>
          <div class="text-sm text-slate-400">Status: ${escapeHtml(b.status)}</div>
          <div class="text-xs text-slate-500">Expires: ${escapeHtml(b.expiresAt || "-")}</div>
        </div>
        <div class="flex gap-2">${cancelBtn}</div>
      </div>
    </div>
  `;
}

function adminUserRow(u) {
  return `
    <div class="bg-slate-950/40 border border-slate-800 rounded-2xl p-3">
      <div class="flex items-start justify-between gap-3">
        <div>
          <div class="font-semibold">${escapeHtml(u.fullName)}</div>
          <div class="text-xs text-slate-500">${escapeHtml(u.email)}</div>
          <div class="text-sm text-slate-400">Status: ${escapeHtml(u.status)}</div>
        </div>
        <div class="flex gap-2">
          <button class="px-3 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-sm"
            onclick="window.__adminApproveUser(${u.id})">Approve</button>
          <button class="px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-sm border border-slate-700"
            onclick="window.__adminRejectUser(${u.id})">Reject</button>
        </div>
      </div>
    </div>
  `;
}

function adminBookingRow(b) {
  const user = b.user;
  return `
    <div class="bg-slate-950/40 border border-slate-800 rounded-2xl p-3">
      <div class="flex items-start justify-between gap-3">
        <div>
          <div class="font-semibold">Booking #${escapeHtml(b.id)} (Slot ${escapeHtml(b.slotId)})</div>
          <div class="text-xs text-slate-500">
            User: ${escapeHtml(user?.fullName || "-")} (${escapeHtml(user?.email || "-")})
          </div>
          <div class="text-xs text-slate-500">Expires: ${escapeHtml(b.expiresAt || "-")}</div>
        </div>
        <div class="flex gap-2">
          <button class="px-3 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-sm"
            onclick="window.__adminApproveBooking(${b.id})">Approve + Open Gate</button>
          <button class="px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-sm border border-slate-700"
            onclick="window.__adminRejectBooking(${b.id})">Reject</button>
        </div>
      </div>
    </div>
  `;
}

async function refreshSlotsAndRole() {
  const slotStatus = document.getElementById("slotStatus");
  const slotsEl = document.getElementById("slots");
  const adminSection = document.getElementById("adminSection");

  try {
    const data = await getJson("/api/slots");
    slotStatus.textContent = `Device: ${data.deviceId} | TTL: ${data.bookingTTLMin}min`;
    slotsEl.innerHTML = data.slots.map(slotCard).join("");

    // show admin section if admin
    if (data.userRole === "admin") adminSection.classList.remove("hidden");
    else adminSection.classList.add("hidden");

  } catch (e) {
    slotStatus.textContent = "Failed";
    slotsEl.innerHTML = `<div class="text-rose-200">${escapeHtml(e.message)}</div>`;
  }
}

async function refreshMyBookings() {
  const el = document.getElementById("myBookings");
  const st = document.getElementById("bookingStatus");
  try {
    const data = await getJson("/api/bookings/me");
    st.textContent = `Total: ${data.length}`;
    el.innerHTML = data.map(bookingRow).join("") || `<div class="text-sm text-slate-400">No bookings yet.</div>`;
  } catch (e) {
    st.textContent = "Failed";
    el.innerHTML = `<div class="text-rose-200">${escapeHtml(e.message)}</div>`;
  }
}

async function refreshAdmin() {
  const usersSt = document.getElementById("adminUsersStatus");
  const usersEl = document.getElementById("pendingUsers");
  const bSt = document.getElementById("adminBookingsStatus");
  const bEl = document.getElementById("pendingBookings");

  // If not admin, these endpoints will 403; that's fine
  try {
    const users = await getJson("/api/admin/users/pending");
    usersSt.textContent = `Pending: ${users.length}`;
    usersEl.innerHTML = users.map(adminUserRow).join("") || `<div class="text-sm text-slate-400">No pending users.</div>`;
  } catch {
    // hide errors if user is not admin
    usersSt.textContent = "";
    usersEl.innerHTML = "";
  }

  try {
    const bs = await getJson("/api/admin/bookings/pending");
    bSt.textContent = `Pending: ${bs.length}`;
    bEl.innerHTML = bs.map(adminBookingRow).join("") || `<div class="text-sm text-slate-400">No pending bookings.</div>`;
  } catch {
    bSt.textContent = "";
    bEl.innerHTML = "";
  }
}

async function refreshAll() {
  await refreshSlotsAndRole();
  await refreshMyBookings();
  await refreshAdmin();
}

refreshAll();
setInterval(refreshAll, 2000);
