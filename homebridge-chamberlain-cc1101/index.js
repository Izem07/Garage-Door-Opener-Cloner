"use strict";

/**
 * Halo for Homebridge.
 *
 * Exposes two things in Apple Home:
 *   - Garage Door: follows Halo's live state (Server-Sent Events, with polling
 *     as a fallback), so a door opened by the wall button or a handheld remote
 *     shows up within about a second.
 *   - Garage Light: a momentary switch. The opener light is a toggle and Halo
 *     can't see it, so tapping the switch presses the light button and the
 *     switch flips back off. Siri: "turn on Garage Light".
 *
 * Halo is the single source of truth. This plugin never writes Halo's state file.
 * All traffic stays on the local machine (default http://127.0.0.1:8080).
 *
 * Legacy "script" mode (no Halo server) still works: set scriptPath and leave
 * apiUrl empty.
 */

const http = require("http");
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");

const PLUGIN_NAME = "homebridge-chamberlain-cc1101";
const ACCESSORY_NAME = "ChamberlainCC1101";
let hap;

module.exports = (api) => {
  hap = api.hap;
  api.registerAccessory(PLUGIN_NAME, ACCESSORY_NAME, HaloGarage);
};

class HaloGarage {
  constructor(log, config) {
    const { Service, Characteristic } = hap;
    this.log = log;
    this.config = config || {};
    this.name = this.config.name || "Garage Door";
    this.lightName = this.config.lightName || "Garage Light";
    this.showLight = this.config.showLight !== false;

    // Mode: talk to the Halo server (default) or run garage_door.py directly.
    this.scriptPath = this.config.scriptPath || null;
    this.apiUrl = this.config.apiUrl || (this.scriptPath ? null : "http://127.0.0.1:8080");
    this.travelTime = Math.max(3, parseInt(this.config.doorTravelTime || 12, 10));

    const C = Characteristic;
    this.DOOR = {
      OPEN: [C.CurrentDoorState.OPEN, C.TargetDoorState.OPEN],
      OPENING: [C.CurrentDoorState.OPENING, C.TargetDoorState.OPEN],
      CLOSING: [C.CurrentDoorState.CLOSING, C.TargetDoorState.CLOSED],
      CLOSED: [C.CurrentDoorState.CLOSED, C.TargetDoorState.CLOSED],
    };
    this.current = null;   // pushed to HomeKit below, so Home never shows HAP's default ("open")
    this.target = null;
    this.haloState = "CLOSED";
    this.lightBusy = false;
    this.lastLightDone = 0;

    // ── Services ────────────────────────────────────────────────────────────
    this.info = new Service.AccessoryInformation()
      .setCharacteristic(C.Manufacturer, "Halo")
      .setCharacteristic(C.Model, "Security+ 1.0 (315 MHz)")
      .setCharacteristic(C.SerialNumber, "HALO-CC1101")
      .setCharacteristic(C.FirmwareRevision, require("./package.json").version);

    this.door = new Service.GarageDoorOpener(this.name);
    this._onGet(this.door.getCharacteristic(C.CurrentDoorState), () => this.current);
    this._onGet(this.door.getCharacteristic(C.TargetDoorState), () => this.target);
    this._onSet(this.door.getCharacteristic(C.TargetDoorState), (v) => this._setTarget(v));
    this._onGet(this.door.getCharacteristic(C.ObstructionDetected), () => false);

    if (this.showLight) {
      this.light = new Service.Switch(this.lightName, "light");
      if (C.ConfiguredName) this.light.setCharacteristic(C.ConfiguredName, this.lightName);
      this._onGet(this.light.getCharacteristic(C.On), () => false);
      this._onSet(this.light.getCharacteristic(C.On), () => this._pressLight());
    }

    this._applyHaloState("CLOSED", "starting");

    // ── Keep in sync with Halo ──────────────────────────────────────────────
    if (this.apiUrl) {
      this._connectEvents();
    } else {
      this._watchStateFile();
    }
  }

  getServices() {
    return [this.info, this.door, this.light].filter(Boolean);
  }

  // ── HAP handler registration (Homebridge 1.x and 2.x) ─────────────────────
  _onGet(char, fn) {
    if (typeof char.onGet === "function") return char.onGet(fn);
    char.on("get", (cb) => { try { cb(null, fn()); } catch (e) { cb(e); } });
  }

  _onSet(char, fn) {
    if (typeof char.onSet === "function") return char.onSet(async (v) => { await fn(v); });
    char.on("set", (v, cb) => Promise.resolve(fn(v)).then(() => cb(null), (e) => cb(e)));
  }

  _hapError(status) {
    if (hap.HapStatusError && hap.HAPStatus) return new hap.HapStatusError(status);
    return new Error("HAP error " + status);
  }

  // ── Door ──────────────────────────────────────────────────────────────────
  _applyHaloState(state, why) {
    const pair = this.DOOR[state];
    if (!pair) return;
    this.haloState = state;
    const [cur, tgt] = pair;
    const C = hap.Characteristic;
    if (tgt !== this.target) {
      this.target = tgt;
      this.door.getCharacteristic(C.TargetDoorState).updateValue(tgt);
    }
    if (cur !== this.current) {
      this.current = cur;
      this.door.getCharacteristic(C.CurrentDoorState).updateValue(cur);
      this.log.info(`${this.name} → ${state.toLowerCase()}${why ? ` (${why})` : ""}`);
    }
  }

  async _setTarget(value) {
    const C = hap.Characteristic;
    const wantOpen = value === C.TargetDoorState.OPEN;
    this.log.info(`Home app → ${wantOpen ? "open" : "close"} ${this.name}`);

    if (!this.apiUrl) return this._scriptDoor(wantOpen);

    try {
      const r = await this._post(wantOpen ? "/api/open" : "/api/close", { source: "HomeKit" });
      if (r && r.door_state) this._applyHaloState(r.door_state, "command sent");
    } catch (e) {
      const before = this.haloState;
      setImmediate(() => {           // put Home back to what the door is really doing
        this.target = -1; this.current = -1;
        this._applyHaloState(before, "command failed");
      });
      if (e.status === 423) {
        this.log.warn(`${this.name}: blocked, Halo's remote lock is on`);
        throw this._hapError(hap.HAPStatus && hap.HAPStatus.NOT_ALLOWED_IN_CURRENT_STATE);
      }
      this.log.error(`${this.name}: ${e.message}`);
      throw this._hapError(hap.HAPStatus && hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE);
    }
  }

  // ── Light (momentary toggle) ──────────────────────────────────────────────
  async _pressLight() {
    const C = hap.Characteristic;
    // Flip the switch back off once HomeKit has stored this write (it does so after we return).
    const flipBack = () => setTimeout(() => this.light.getCharacteristic(C.On).updateValue(false), 500);
    // A tap can arrive twice (Home + Siri, or a retry): one press per tap.
    if (this.lightBusy || Date.now() - this.lastLightDone < 1000) return flipBack();
    this.lightBusy = true;
    this.log.info(`Home app → toggle ${this.lightName}`);
    try {
      if (this.apiUrl) await this._post("/api/light", { source: "HomeKit" });
      else await this._exec("light");
    } catch (e) {
      this.log.error(`${this.lightName}: ${e.message}`);
      throw this._hapError(hap.HAPStatus && hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE);
    } finally {
      this.lightBusy = false;
      this.lastLightDone = Date.now();
      flipBack();
    }
  }

  // ── Live updates from Halo: SSE, falling back to polling ─────────────────
  _connectEvents() {
    let retry = 1000;
    let pollTimer = null;
    const startPolling = () => {
      if (pollTimer) return;
      pollTimer = setInterval(() => {
        this._get("/api/status").then((s) => this._applyHaloState(s.door && s.door.state, "poll"),
          () => {});
      }, 3000);
    };
    const stopPolling = () => { clearInterval(pollTimer); pollTimer = null; };

    const connect = () => {
      const url = new URL("/api/events", this.apiUrl);
      const req = http.get({ hostname: url.hostname, port: url.port || 80, path: url.pathname,
        headers: { Accept: "text/event-stream" }, agent: false }, (res) => {
        if (res.statusCode !== 200) { res.resume(); return retryLater(); }
        retry = 1000;
        stopPolling();
        this.log.debug("Connected to Halo live updates");
        let buf = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          buf += chunk;
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const evt = buf.slice(0, i);
            buf = buf.slice(i + 2);
            const data = evt.split("\n").filter((l) => l.startsWith("data:"))
              .map((l) => l.slice(5).trim()).join("");
            if (!data) continue;
            try {
              const s = JSON.parse(data);
              this._applyHaloState(s.door && s.door.state, "live");
            } catch (e) { /* ignore malformed event */ }
          }
        });
        res.on("end", retryLater);
        res.on("error", retryLater);
      });
      // Halo sends a keep-alive every 15 s; 40 s of silence means the link is dead.
      req.setTimeout(40000, () => req.destroy(new Error("silent")));
      req.on("error", retryLater);
    };

    let pending = false;
    const retryLater = () => {
      if (pending) return;
      pending = true;
      startPolling();
      setTimeout(() => { pending = false; connect(); }, retry);
      retry = Math.min(retry * 2, 5000);
    };
    connect();
  }

  // ── HTTP helpers (local only, short timeouts) ─────────────────────────────
  _request(method, endpoint, body, timeoutMs) {
    return new Promise((resolve, reject) => {
      const url = new URL(endpoint, this.apiUrl);
      const payload = body ? JSON.stringify(body) : "";
      const req = http.request({
        hostname: url.hostname, port: url.port || 80, path: url.pathname + url.search, method,
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) },
        agent: false,
      }, (res) => {
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          let parsed = {};
          try { parsed = JSON.parse(data || "{}"); } catch (e) { /* non-JSON */ }
          if (res.statusCode >= 200 && res.statusCode < 300) return resolve(parsed);
          const err = new Error(parsed.error || `HTTP ${res.statusCode}`);
          err.status = res.statusCode;
          reject(err);
        });
      });
      req.setTimeout(timeoutMs, () => req.destroy(new Error(`Halo did not respond within ${timeoutMs / 1000} s`)));
      req.on("error", reject);
      req.end(payload);
    });
  }

  _get(endpoint) { return this._request("GET", endpoint, null, 3000); }
  // Commands wait for the radio burst (~2 s), so allow a little longer.
  _post(endpoint, body) { return this._request("POST", endpoint, body, 6000); }

  // ── Legacy script mode (no Halo server) ──────────────────────────────────
  _exec(action) {
    return new Promise((resolve, reject) => {
      execFile("python3", [this.scriptPath, action], { timeout: 20000 }, (err, stdout) =>
        err ? reject(err) : resolve(stdout.trim()));
    });
  }

  async _scriptDoor(wantOpen) {
    this._applyHaloState(wantOpen ? "OPENING" : "CLOSING", "command sent");
    try {
      await this._exec(wantOpen ? "open" : "close");
    } catch (e) {
      this.log.error(`${this.name}: ${e.message}`);
      throw this._hapError(hap.HAPStatus && hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE);
    }
    clearTimeout(this.travelTimer);
    this.travelTimer = setTimeout(() => this._applyHaloState(wantOpen ? "OPEN" : "CLOSED", "travel time"),
      this.travelTime * 1000);
  }

  _watchStateFile() {
    const file = path.join(this.config.stateDir || "/homebridge", "garage_state.json");
    const read = () => {
      try {
        const s = JSON.parse(fs.readFileSync(file, "utf8"));
        if (s.doorState) this._applyHaloState(s.doorState, "state file");
      } catch (e) { /* missing or mid-write: try next time */ }
    };
    read();
    fs.watchFile(file, { interval: 1000 }, read);
  }
}
