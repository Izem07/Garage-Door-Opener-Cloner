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
    this.doorTravelTime = parseInt(this.config.doorTravelTime || 12, 10); // in seconds
    this.stateFilePath = path.join(
      this.config.stateDir || "/homebridge",
      "garage_state.json"
    );

    // Initial state: 1 = CLOSED, 0 = OPEN
    this.currentDoorState = Characteristic.CurrentDoorState.CLOSED;
    this.targetDoorState = Characteristic.TargetDoorState.CLOSED;
    this.obstructionDetected = false;
    this.operatingTimer = null;

    // Load persisted state if available
    this.loadState();

    // Create HomeKit Garage Door Opener Service
    this.service = new Service.GarageDoorOpener(this.name);

    this.service
      .getCharacteristic(Characteristic.CurrentDoorState)
      .onGet(this.getCurrentDoorState.bind(this));

    this.service
      .getCharacteristic(Characteristic.TargetDoorState)
      .onGet(this.getTargetDoorState.bind(this))
      .onSet(this.setTargetDoorState.bind(this));

    this.service
      .getCharacteristic(Characteristic.ObstructionDetected)
      .onGet(this.getObstructionDetected.bind(this));

    // Create HomeKit Light Service (controls opener courtesy light)
    this.lightOn = false;
    this.lightService = new Service.Lightbulb(`${this.name} Light`);
    this.lightService
      .getCharacteristic(Characteristic.On)
      .onGet(() => this.lightOn)
      .onSet(this.setLightState.bind(this));

    // Information service
    this.infoService = new Service.AccessoryInformation()
      .setCharacteristic(Characteristic.Manufacturer, "Chamberlain / TI CC1101")
      .setCharacteristic(Characteristic.Model, "Security+ 1.0 (315MHz)")
      .setCharacteristic(Characteristic.SerialNumber, "CC1101-PI-01");

    // Watch garage_state.json for ground-truth updates (e.g. from Wyze contact sensor)
    try {
      if (fs.existsSync(this.stateFilePath)) {
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
              `[${this.name}] Wyze contact sensor updated door state to: ${
                this.currentDoorState === Characteristic.CurrentDoorState.OPEN ? "OPEN" : "CLOSED"
              }`
            );
          }
        });
      }
    } catch (e) {
      this.log.warn(`[${this.name}] File watch failed: ${e.message}`);
    }

    // Periodic HTTP sync fallback if running in API bridge mode
    if (this.apiUrl) {
      setInterval(() => {
        if (this.operatingTimer) return;
        this._sendHttpRequest("/api/status", "GET", (err, data) => {
          if (err) return;
          try {
            const parsed = JSON.parse(data);
            const doorStateStr = parsed.door && parsed.door.state;
            if (doorStateStr && (doorStateStr === "OPEN" || doorStateStr === "CLOSED")) {
              const isOpen = doorStateStr === "OPEN";
              const newCurrentState = isOpen
                ? Characteristic.CurrentDoorState.OPEN
                : Characteristic.CurrentDoorState.CLOSED;
              const newTargetState = isOpen
                ? Characteristic.TargetDoorState.OPEN
                : Characteristic.TargetDoorState.CLOSED;

              if (this.currentDoorState !== newCurrentState) {
                this.currentDoorState = newCurrentState;
                this.targetDoorState = newTargetState;
                this.service
                  .getCharacteristic(Characteristic.CurrentDoorState)
                  .updateValue(this.currentDoorState);
                this.service
                  .getCharacteristic(Characteristic.TargetDoorState)
                  .updateValue(this.targetDoorState);
                this.log.info(
                  `[${this.name}] Ground-truth sensor synced door state to: ${doorStateStr}`
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
          } catch (e) {}
        });
      }, 3000);
    }
  }

  getServices() {
    return [this.infoService, this.service, this.lightService];
  }

  _sendHttpRequest(endpoint, method, callback) {
    if (typeof method === "function") {
      callback = method;
      method = "POST";
    }
    const httpMethod = (method || "POST").toUpperCase();
    const url = new URL(endpoint, this.apiUrl);
    const options = {
      hostname: url.hostname,
      port: url.port || 80,
      path: url.pathname,
      method: httpMethod,
      headers: {
        "Content-Type": "application/json",
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
          callback(new Error(`HTTP status ${res.statusCode}: ${data}`));
        }
      });
    });

    req.on("error", (err) => callback(err));
    req.on("timeout", () => {
      req.destroy();
      callback(new Error("HTTP request timed out"));
    });

    if (httpMethod === "POST") {
      req.write(JSON.stringify({}));
    }
    req.end();
  }

  triggerAction(action, callback) {
    if (this.apiUrl) {
      const endpoint = action === "light" ? "/api/light" : "/api/toggle";
      this.log.info(`[${this.name}] Sending HTTP POST to ${this.apiUrl}${endpoint}`);
      this._sendHttpRequest(endpoint, callback);
    } else {
      const cmd = `python3 "${this.scriptPath}" ${action}`;
      this.log.info(`[${this.name}] Executing command: ${cmd}`);
      exec(cmd, (error, stdout) => {
        if (error) {
          callback(error);
        } else {
          callback(null, stdout);
        }
      });
    }
  }

  setLightState(value) {
    this.lightOn = value;
    this.log.info(`[${this.name}] Requested Light state: ${value ? "ON" : "OFF"}`);
    this.triggerAction("light", (error, result) => {
      if (error) {
        this.log.error(`[${this.name}] Failed to toggle light: ${error.message}`);
        return;
      }
      this.log.info(`[${this.name}] Opener light toggled: ${result}`);
    });
  }

  loadState() {
    try {
      if (fs.existsSync(this.stateFilePath)) {
        const raw = fs.readFileSync(this.stateFilePath, "utf8");
        const data = JSON.parse(raw);
        if (data.doorState === "OPEN") {
          this.currentDoorState = Characteristic.CurrentDoorState.OPEN;
          this.targetDoorState = Characteristic.TargetDoorState.OPEN;
        } else {
          this.currentDoorState = Characteristic.CurrentDoorState.CLOSED;
          this.targetDoorState = Characteristic.TargetDoorState.CLOSED;
        }
        this.log.info(
          `[${this.name}] Loaded saved state: ${data.doorState || "CLOSED"}`
        );
      }
    } catch (e) {
      this.log.warn(`[${this.name}] Could not load saved state: ${e.message}`);
    }
  }

  saveState(stateStr) {
    try {
      fs.writeFileSync(
        this.stateFilePath,
        JSON.stringify({ doorState: stateStr, updatedAt: new Date().toISOString() }, null, 2),
        "utf8"
      );
    } catch (e) {
      this.log.warn(`[${this.name}] Could not save state: ${e.message}`);
    }
  }

  getCurrentDoorState() {
    return this.currentDoorState;
  }

  getTargetDoorState() {
    return this.targetDoorState;
  }

  getObstructionDetected() {
    return this.obstructionDetected;
  }

  setTargetDoorState(value) {
    this.log.info(
      `[${this.name}] HomeKit requested target state: ${
        value === Characteristic.TargetDoorState.OPEN ? "OPEN" : "CLOSED"
      }`
    );

    // If already in target state or moving towards it, avoid redundant triggers
    if (value === this.targetDoorState && value === this.currentDoorState) {
      this.log.info(`[${this.name}] Door is already in requested state.`);
      return;
    }

    this.targetDoorState = value;

    if (this.operatingTimer) {
      clearTimeout(this.operatingTimer);
      this.operatingTimer = null;
    }

    const isOpening = value === Characteristic.TargetDoorState.OPEN;

    // Set intermediate state: OPENING or CLOSING
    this.currentDoorState = isOpening
      ? Characteristic.CurrentDoorState.OPENING
      : Characteristic.CurrentDoorState.CLOSING;

    this.service
      .getCharacteristic(Characteristic.CurrentDoorState)
      .updateValue(this.currentDoorState);

    // Execute toggle
    this.triggerAction("toggle", (error, result) => {
      if (error) {
        this.log.error(`[${this.name}] Failed to toggle door: ${error.message}`);
        this.currentDoorState = Characteristic.CurrentDoorState.STOPPED;
        this.service
          .getCharacteristic(Characteristic.CurrentDoorState)
          .updateValue(this.currentDoorState);
        return;
      }

      this.log.info(`[${this.name}] Toggle action triggered: ${result}`);

      // Wait for door travel time to complete, then update to final state
      this.operatingTimer = setTimeout(() => {
        this.currentDoorState = isOpening
          ? Characteristic.CurrentDoorState.OPEN
          : Characteristic.CurrentDoorState.CLOSED;

        this.service
          .getCharacteristic(Characteristic.CurrentDoorState)
          .updateValue(this.currentDoorState);

        this.saveState(isOpening ? "OPEN" : "CLOSED");
        this.log.info(
          `[${this.name}] Door movement completed: ${
            isOpening ? "OPEN" : "CLOSED"
          }`
        );
        this.operatingTimer = null;
      }, this.doorTravelTime * 1000);
    });
  }
}

