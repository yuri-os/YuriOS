/* The puppet-strings client (SPEC §4, §10) — port of the vrm-viewer
 * ControlBridge.ts, re-plumbed onto the one bus: instead of owning a
 * WebSocket, it listens for the `world-ev` events chat.js re-dispatches from
 * /api/events and realises the `avatar` ones on the stage. The dispatch table
 * is the same channel set as ever — Build #4's two scene channels (`rain`,
 * `music`) included; a command's old wire `type` is the event's `op`. */
export class ControlBridge {
  constructor(stage, { room, music } = {}) {
    this.stage = stage;
    this.room = room;       // SanctuaryScene (SPEC §6) — rain intensity
    this.music = music;     // music.js — play/stop ambience (SPEC §7.5)
    this._handler = null;
  }

  listen() {
    if (this._handler) return;
    this._handler = (e) => {
      const m = e.detail;
      if (m && m.type === 'avatar') this.dispatch(m);
    };
    window.addEventListener('world-ev', this._handler);
  }

  stop() {
    if (this._handler) window.removeEventListener('world-ev', this._handler);
    this._handler = null;
  }

  dispatch(cmd) {
    switch (cmd.op) {
      case 'expression':
        // puppet-channel semantics: auto-reset to neutral (vrm-viewer's 3 s);
        // turn expressions arrive with reset_ms 0 — hold until the next (B2 §6)
        this.stage.setExpression(cmd.name, cmd.intensity ?? 1, cmd.reset_ms ?? 3000);
        break;
      case 'expression_raw':
        this.stage.setExpressionRaw(cmd.values);
        break;
      case 'look_at':
        if (cmd.mode) this.stage.lookAtMode(cmd.mode);
        else this.stage.lookAtTarget(cmd.target);
        break;
      case 'bone':
        this.stage.setBone(cmd.name, cmd.euler);
        break;
      case 'bone_reset':
        this.stage.resetBone(cmd.name);
        break;
      case 'mouth':
        this.stage.setMouth(cmd.value);
        break;
      case 'material_color':
        this.stage.setMaterialColor(cmd.material, cmd.color);
        break;
      case 'animation':
        void this.stage.playAnimation(cmd.url, cmd.loop ?? true, cmd.fadeIn ?? 0.3);
        break;
      case 'load_model':
        void this.stage.loadModel(cmd.url);
        break;
      case 'rain':                                     // SPEC §4/§6 — scene channel
        this.room?.setRain(cmd.intensity);
        this.music?.setRainLevel?.(cmd.intensity);
        break;
      case 'music':                                    // SPEC §4/§7.5 — ambience
        if (cmd.action === 'play') this.music?.play(cmd.track, cmd.volume);
        else this.music?.stop();
        break;
      default:
        console.warn('[ControlBridge] unknown avatar op:', cmd);
    }
  }
}
