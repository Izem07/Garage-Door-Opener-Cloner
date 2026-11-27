"use strict";

const { exec } = require("child_process");
const http = require("http");
const fs = require("fs");
const path = require("path");

let Service, Characteristic;

module.exports = (homebridge) => {
  Service = homebridge.hap.Service;
  Characteristic = homebridge.hap.Characteristic;

  homebridge.registerAccessory(
    "homebridge-chamberlain-cc1101",
    "ChamberlainCC1101",
    ChamberlainCC1101Accessory
  );
};

class ChamberlainCC1101Accessory {
  constructor(log, config) {
    this.log = log;
    this.config = config || {};

    this.name = this.config.name || "Garage Door";
    this.apiUrl = this.config.apiUrl || null;
    this.scriptPath =
      this.config.scriptPath ||
      "/home/izem07/Garage-Door-Opener-Cloner/garage_door.py";
    this.doorTravelTime = parseInt(this.config.doorTravelTime || 12, 10);
    this.stateFilePath = path.join(
      this.config.stateDir || "/homebridge",
      "garage_state.json"
    );

    // Internal state — initialise before loadState() reads from disk
    this.currentDoorState = Characteristic.CurrentDoorState.CLOSED;
    this.targetDoorState  = Characteristic.TargetDoorState.CLOSED;
    this.obstructionDetected = false;
    this.lightOn = false;
    this.operatingTimer = null;

    this.loadState();

    // ── Garage Door Opener service ─────────────────────────────────────────
    this.service = new Service.GarageDoorOpener(this.name);

    // CurrentDoorState – read-only
    this._bindChar(
      this.service.getCharacteristic(Characteristic.CurrentDoorState),
      () => this.currentDoorState,
      null
    );

    // TargetDoorState – read + write
    this._bindChar(
      this.service.getCharacteristic(Characteristic.TargetDoorState),
      () => this.targetDoorState,
      (value) => { this._handleSetTarget(value); }
    );

    // ObstructionDetected – read-only
    this._bindChar(
      this.service.getCharacteristic(Characteristic.ObstructionDetected),
      () => this.obstructionDetected,
      null
    );

    // ── Light (opener courtesy bulb) service ──────────────────────────────
    this.lightService = new Service.Lightbulb(`${this.name} Light`);
    this._bindChar(
      this.lightService.getCharacteristic(Characteristic.On),
      () => this.lightOn,
      (value) => { this._handleSetLight(value); }
    );

    // ── Accessory Information ─────────────────────────────────────────────
    this.infoService = new Service.AccessoryInformation()
      .setCharacteristic(Characteristic.Manufacturer, "Chamberlain / TI CC1101")
      .setCharacteristic(Characteristic.Model, "Security+ 1.0 (315 MHz)")
      .setCharacteristic(Characteristic.SerialNumber, "CC1101-PI-01");

    // ── Ground-truth sync: file watcher ──────────────────────────────────
    try {
      // Watch always; create the file first if it doesn't exist yet
      if (!fs.existsSync(this.stateFilePath)) {
        const dir = path.dirname(this.stateFilePath);
        if (fs.existsSync(dir)) {
          fs.writeFileSync(
            this.stateFilePath,
            JSON.stringify({ doorState: "CLOSED", updatedAt: new Date().toISOString() }, null, 2)
          );
        }
      }
      fs.watchFile(this.stateFilePath, { interval: 1000 }, () => {
        const oldState = this.currentDoorState;
        this.loadState();
        if (oldState !== this.currentDoorState) {
          this.service
            .getCharacteristic(Characteristic.CurrentDoorState)
            .updateValue(this.currentDoorState);
          this.service
            .getCharacteristic(Characteristic.TargetDoorState)
            .updateValue(this.targetDoorState);
          this.log.info(
            `[${this.name}] Contact sensor → door is now: ${
              this.currentDoorState === Characteristic.CurrentDoorState.OPEN ? "OPEN" : "CLOSED"
            }`
          );
        }
      });
    } catch (e) {
      this.log.warn(`[${this.name}] File watch failed: ${e.message}`);
    }

    // ── Ground-truth sync: HTTP poll (api bridge mode, every 3 s) ─────────
    if (this.apiUrl) {
      setInterval(() => {
        if (this.operatingTimer) return; // don't interfere while door is moving
        this._getJson("/api/status", (err, parsed) => {
          if (err) return;

          const doorStateStr = parsed.door && parsed.door.state;
          if (doorStateStr === "OPEN" || doorStateStr === "CLOSED") {
            const isOpen = doorStateStr === "OPEN";
            const newCurrent = isOpen
              ? Characteristic.CurrentDoorState.OPEN
              : Characteristic.CurrentDoorState.CLOSED;
            const newTarget = isOpen
              ? Characteristic.TargetDoorState.OPEN
              : Characteristic.TargetDoorState.CLOSED;

            if (this.currentDoorState !== newCurrent) {
              this.currentDoorState = newCurrent;
              this.targetDoorState  = newTarget;
              this.service
                .getCharacteristic(Characteristic.CurrentDoorState)
                .updateValue(newCurrent);
              this.service
                .getCharacteristic(Characteristic.TargetDoorState)
                .updateValue(newTarget);
              this.log.info(
                `[${this.name}] HTTP poll → door is now: ${doorStateStr}`
              );
            }
          }

          if (parsed.light && typeof parsed.light.on === "boolean") {
            if (this.lightOn !== parsed.light.on) {
              this.lightOn = parsed.light.on;
              this.lightService
                .getCharacteristic(Characteristic.On)
                .updateValue(this.lightOn);
            }
          }
        });
      }, 3000);
    }
  }

  // ── HAP v1/v2 compatibility shim ────────────────────────────────────────
  // Homebridge 2.x (HAP-Node.js v13+) uses onGet/onSet returning values/Promises.
  // Homebridge 1.x uses the classic .on('get', cb) event-emitter style.
  // We register both; HAP ignores the one it doesn't understand.
  _bindChar(char, getter, setter) {
    // Modern Homebridge 2.x API
    if (typeof char.onGet === "function" && getter) {
      char.onGet(getter);
    }
    if (typeof char.onSet === "function" && setter) {
      char.onSet(async (value) => { setter(value); });
    }

    // Legacy Homebridge 1.x callback API — also fires in 2.x as a fallback
    if (getter) {
      char.on("get", (cb) => {
        try { cb(null, getter()); }
        catch (e) { cb(e); }
      });
    }
    if (setter) {
      char.on("set", (value, cb) => {
        try { setter(value); cb(null); }
        catch (e) { cb(e); }
      });
    }
  }

  getServices() {
    return [this.infoService, this.service, this.lightService];
  }

  // ── State persistence ────────────────────────────────────────────────────
  loadState() {
    try {
      if (fs.existsSync(this.stateFilePath)) {
        const raw  = fs.readFileSync(this.stateFilePath, "utf8");
        const data = JSON.parse(raw);
        if (data.doorState === "OPEN") {
          this.currentDoorState = Characteristic.CurrentDoorState.OPEN;
          this.targetDoorState  = Characteristic.TargetDoorState.OPEN;
        } else {
          this.currentDoorState = Characteristic.CurrentDoorState.CLOSED;
          this.targetDoorState  = Characteristic.TargetDoorState.CLOSED;
        }
        if (typeof data.lightOn === "boolean") this.lightOn = data.lightOn;
        this.log.info(`[${this.name}] Loaded state: ${data.doorState || "CLOSED"}`);
      }
    } catch (e) {
      this.log.warn(`[${this.name}] Could not load saved state: ${e.message}`);
    }
  }

  saveState(stateStr) {
    try {
      const payload = {
        doorState:  stateStr,
        lightOn:    this.lightOn,
        updatedAt:  new Date().toISOString(),
      };
      fs.writeFileSync(this.stateFilePath, JSON.stringify(payload, null, 2), "utf8");
    } catch (e) {
      this.log.warn(`[${this.name}] Could not save state: ${e.message}`);
    }
  }

  // ── Door toggle ──────────────────────────────────────────────────────────
  _handleSetTarget(value) {
    this.log.info(
      `[${this.name}] HomeKit → target: ${
        value === Characteristic.TargetDoorState.OPEN ? "OPEN" : "CLOSED"
      }`
    );

    if (value === this.targetDoorState && value === this.currentDoorState) {
      this.log.info(`[${this.name}] Door already in requested state.`);
      return;
    }

    this.targetDoorState = value;

    if (this.operatingTimer) {
      clearTimeout(this.operatingTimer);
      this.operatingTimer = null;
    }

    const isOpening = value === Characteristic.TargetDoorState.OPEN;

    this.currentDoorState = isOpening
      ? Characteristic.CurrentDoorState.OPENING
      : Characteristic.CurrentDoorState.CLOSING;

    this.service
      .getCharacteristic(Characteristic.CurrentDoorState)
      .updateValue(this.currentDoorState);

    this._postAction("toggle", (error, result) => {
      if (error) {
        this.log.error(`[${this.name}] Toggle failed: ${error.message}`);
        this.currentDoorState = Characteristic.CurrentDoorState.STOPPED;
        this.service
          .getCharacteristic(Characteristic.CurrentDoorState)
          .updateValue(this.currentDoorState);
        return;
      }

      this.log.info(`[${this.name}] Toggle OK: ${result}`);

      this.operatingTimer = setTimeout(() => {
        this.currentDoorState = isOpening
          ? Characteristic.CurrentDoorState.OPEN
          : Characteristic.CurrentDoorState.CLOSED;
        this.service
          .getCharacteristic(Characteristic.CurrentDoorState)
          .updateValue(this.currentDoorState);
        this.saveState(isOpening ? "OPEN" : "CLOSED");
        this.log.info(
          `[${this.name}] Travel done → ${isOpening ? "OPEN" : "CLOSED"}`
        );
        this.operatingTimer = null;
      }, this.doorTravelTime * 1000);
    });
  }

  // ── Light toggle ─────────────────────────────────────────────────────────
  _handleSetLight(value) {
    this.lightOn = !!value;
    this.log.info(`[${this.name}] Light → ${this.lightOn ? "ON" : "OFF"}`);
    this._postAction("light", (error, result) => {
      if (error) {
        this.log.error(`[${this.name}] Light toggle failed: ${error.message}`);
      } else {
        this.log.info(`[${this.name}] Light OK: ${result}`);
      }
    });
  }

  // ── HTTP helpers ─────────────────────────────────────────────────────────
  _postAction(action, callback) {
    if (this.apiUrl) {
      const endpoint = action === "light" ? "/api/light" : "/api/toggle";
      this.log.info(`[${this.name}] POST ${this.apiUrl}${endpoint}`);
      this._httpRequest(endpoint, "POST", null, callback);
    } else {
      const cmd = `python3 "${this.scriptPath}" ${action}`;
      this.log.info(`[${this.name}] exec: ${cmd}`);
      exec(cmd, (error, stdout) => {
        if (error) callback(error);
        else callback(null, stdout.trim());
      });
    }
  }

  _getJson(endpoint, callback) {
    this._httpRequest(endpoint, "GET", null, (err, body) => {
      if (err) return callback(err);
      try { callback(null, JSON.parse(body)); }
      catch (e) { callback(e); }
    });
  }

  _httpRequest(endpoint, method, body, callback) {
    const url = new URL(endpoint, this.apiUrl);
    const bodyStr = body ? JSON.stringify(body) : null;
    const options = {
      hostname: url.hostname,
      port:     parseInt(url.port || "80", 10),
      path:     url.pathname + (url.search || ""),
      method:   method.toUpperCase(),
      headers:  {
        "Content-Type":   "application/json",
        "Content-Length": bodyStr ? Buffer.byteLength(bodyStr) : 0,
      },
      timeout: 5000,
    };

    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          callback(null, data);
        } else {
          callback(new Error(`HTTP ${res.statusCode}: ${data}`));
        }
      });
    });

    req.on("error",   (err) => callback(err));
    req.on("timeout", ()    => { req.destroy(); callback(new Error("HTTP timeout")); });

    if (bodyStr) req.write(bodyStr);
    req.end();
  }
}
