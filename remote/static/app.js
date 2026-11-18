// remote/static/app.js — Tier 23: the phone-side logic for both pages.
// No framework, no build step - one small file, loaded by both login.html
// and index.html, each calling the one init function it needs.
(function () {
  "use strict";

  const DASHBOARD_HEADER = { "X-Wave-Dashboard": "1", "Content-Type": "application/json" };

  function qs(name) {
    const params = new URLSearchParams(window.location.search);
    return params.get(name);
  }

  function initLoginPage() {
    const tokenInput = document.getElementById("token");
    const errEl = document.getElementById("err");
    const goBtn = document.getElementById("go");

    // A QR-code scan lands here with ?token=... already in the URL -
    // prefill and let the person just tap the button, nothing to type.
    const prefill = qs("token");
    if (prefill) tokenInput.value = prefill;

    async function pair() {
      const value = tokenInput.value.trim();
      if (!value) {
        errEl.textContent = "Enter the pairing code first.";
        return;
      }
      goBtn.disabled = true;
      errEl.textContent = "";
      try {
        const res = await fetch("/api/pair", {
          method: "POST",
          headers: DASHBOARD_HEADER,
          body: JSON.stringify({ token: value, code: value }),
        });
        if (res.ok) {
          window.location.href = "/";
          return;
        }
        const data = await res.json().catch(() => ({}));
        errEl.textContent = data.error || "Could not pair - check the code and try again.";
      } catch (e) {
        errEl.textContent = "Could not reach Wave - check you're on the same Wi-Fi.";
      } finally {
        goBtn.disabled = false;
      }
    }

    goBtn.addEventListener("click", pair);
    tokenInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") pair();
    });
  }

  function addBubble(container, text, who, pending) {
    const div = document.createElement("div");
    div.className = "bubble " + who + (pending ? " pending" : "");
    div.textContent = text;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
  }

  function initChatPage() {
    const messages = document.getElementById("messages");
    const input = document.getElementById("text");
    const sendBtn = document.getElementById("send");
    const nameEl = document.getElementById("assistant-name");

    fetch("/api/status")
      .then((r) => r.json())
      .then((data) => {
        if (data && data.assistant_name) nameEl.textContent = data.assistant_name;
        if (data && data.paired === false) window.location.href = "/login";
      })
      .catch(() => {});

    async function send() {
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      sendBtn.disabled = true;
      addBubble(messages, text, "user");
      const pendingBubble = addBubble(messages, "…", "wave", true);
      try {
        const res = await fetch("/api/message", {
          method: "POST",
          headers: DASHBOARD_HEADER,
          body: JSON.stringify({ text: text }),
        });
        if (res.status === 401) {
          window.location.href = "/login";
          return;
        }
        const data = await res.json();
        pendingBubble.textContent = data.reply || data.error || "(no reply)";
        pendingBubble.classList.remove("pending");
      } catch (e) {
        pendingBubble.textContent = "Couldn't reach Wave - check you're still on the same Wi-Fi.";
        pendingBubble.classList.remove("pending");
      } finally {
        sendBtn.disabled = false;
      }
    }

    sendBtn.addEventListener("click", send);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") send();
    });
  }

  window.WaveDashboard = { initLoginPage, initChatPage };
})();
