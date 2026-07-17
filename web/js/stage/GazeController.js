/* Gaze / look-at with idle saccades (SPEC §3.5; CS §5.5) — port of the
 * vrm-viewer GazeController.ts. Modes: 'camera' (eye contact — the default; her
 * eyes find you), 'fixed' (an explicit world point, e.g. the window when she's
 * rain-gazing, SPEC §8.1), 'none' (straight ahead). */
import { Object3D, Vector3 } from 'three';

export class GazeController {
  constructor(camera, eyeHeight) {
    this.camera = camera;
    this.eyeHeight = eyeHeight;
    this.mode = 'camera';
    this.fixation = new Vector3(0, eyeHeight, -1);
    this.desired = new Vector3();
    this.targetNode = undefined;
    this.sinceSaccade = 0;
    this.nextSaccadeAt = 0;
  }

  setMode(mode) {
    this.mode = mode;
  }

  setFixedTarget(t) {
    this.mode = 'fixed';
    this.fixation.set(t.x, t.y, t.z);
  }

  setEyeHeight(y) {
    this.eyeHeight = y;
  }

  update(vrm, delta) {
    if (!vrm.lookAt) return;

    if (!this.targetNode) {
      this.targetNode = new Object3D();
      vrm.lookAt.target = this.targetNode;
    }

    if (this.mode === 'camera')
      this.camera.getWorldPosition(this.desired);
    else if (this.mode === 'none')
      this.desired.set(0, this.eyeHeight, -100);
    else
      this.desired.copy(this.fixation);

    // Idle saccades: small random jitter at random intervals (CS §5.5).
    this.sinceSaccade += delta;
    if (this.sinceSaccade >= this.nextSaccadeAt) {
      this.sinceSaccade = 0;
      this.nextSaccadeAt = Math.random() * 2 + 0.4; // 0.4–2.4 s
      this.desired.x += (Math.random() - 0.5) * 0.25;
      this.desired.y += (Math.random() - 0.5) * 0.25;
    }

    // Smoothly approach the desired point, then let VRM apply it.
    this.targetNode.position.lerp(this.desired, 0.25);
    vrm.lookAt.update(delta);
  }
}
