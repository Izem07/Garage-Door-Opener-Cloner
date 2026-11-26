"use strict";

const { exec } = require("child_process");
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
    this.scriptPath =
      this.config.scriptPath ||
      "/home/izem07/Garage-Door-Opener-Cloner/garage_door.py";
    this.doorTravelTime = parseInt(this.config.doorTravelTime || 12, 10); // in seconds
    this.stateFilePath = path.join(
      this.config.stateDir || path.dirname(this.scriptPath),
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
  }

  getServices() {
    return [this.infoService, this.service, this.lightService];
  }

  setLightState(value) {
    this.lightOn = value;
    this.log.info(`[${this.name}] Requested Light state: ${value ? "ON" : "OFF"}`);
    const cmd = `python3 "${this.scriptPath}" light`;
    this.log.info(`[${this.name}] Executing Light RF command: ${cmd}`);

    exec(cmd, (error, stdout) => {
      if (error) {
        this.log.error(`[${this.name}] Failed to toggle light: ${error.message}`);
        return;
      }
      this.log.info(`[${this.name}] Opener light toggled: ${stdout.trim()}`);
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

    // Execute the Python command to transmit the RF toggle signal
    const cmd = `python3 "${this.scriptPath}" toggle`;
    this.log.info(`[${this.name}] Executing RF command: ${cmd}`);

    exec(cmd, (error, stdout, stderr) => {
      if (error) {
        this.log.error(`[${this.name}] Failed to execute Python toggle: ${error.message}`);
        this.currentDoorState = Characteristic.CurrentDoorState.STOPPED;
        this.service
          .getCharacteristic(Characteristic.CurrentDoorState)
          .updateValue(this.currentDoorState);
        return;
      }

      this.log.info(`[${this.name}] RF transmission complete: ${stdout.trim()}`);

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
