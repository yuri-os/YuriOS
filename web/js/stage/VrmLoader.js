/* Model loading (SPEC §3.3; control spec CS §3) — port of the vrm-viewer
 * reference impl's VrmLoader.ts to no-build ES modules. Same passes, same order;
 * only the types are gone. */
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import { VRMAnimationLoaderPlugin, VRMLookAtQuaternionProxy } from '@pixiv/three-vrm-animation';
import { Box3, Group, Quaternion, Vector3 } from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

// CS §3.1 — one process-wide GLTFLoader registered with the VRM plugins so the
// same loader parses both .vrm models and .vrma animation files.
let loader;

export function getLoader() {
  if (loader) return loader;
  loader = new GLTFLoader();
  loader.crossOrigin = 'anonymous';
  loader.register((parser) => new VRMLoaderPlugin(parser));
  loader.register((parser) => new VRMAnimationLoaderPlugin(parser));
  return loader;
}

// CS §3.2 — load a VRM, run the mandatory perf/normalization passes, and measure
// it for camera framing. Returns { vrm, group, modelSize, modelCenter, eyeHeight }.
export async function loadVrm(url, onProgress) {
  const gltf = await getLoader().loadAsync(url, (e) => {
    if (onProgress && e.total) onProgress(e.loaded / e.total);
  });

  const vrm = gltf.userData.vrm;
  if (!vrm) throw new Error(`File is not a VRM: ${url}`);

  // Big FPS wins — CS §3.2 step 2.
  VRMUtils.removeUnnecessaryVertices(vrm.scene);
  VRMUtils.combineSkeletons(vrm.scene);

  // Avatar parts must never get culled at the frame edge.
  vrm.scene.traverse((o) => { o.frustumCulled = false; });

  // Required for look-at to be drivable; harmless when lookAt is absent.
  if (vrm.lookAt) {
    const proxy = new VRMLookAtQuaternionProxy(vrm.lookAt);
    proxy.name = 'lookAtQuaternionProxy';
    vrm.scene.add(proxy);
  }

  const group = new Group();
  group.add(vrm.scene);

  // Normalize facing so the avatar's faceFront aligns with world -Z (CS §3.2
  // step 6; covers both VRM 0.x and 1.0 without a rotateVRM0 special case).
  if (vrm.lookAt) {
    const target = new Vector3(0, 0, -1);
    const q = new Quaternion().setFromUnitVectors(vrm.lookAt.faceFront.clone().normalize(), target);
    group.quaternion.premultiply(q);
  }

  vrm.springBoneManager?.reset();
  group.updateMatrixWorld(true);

  // Bounding box (skip spring-bone colliders) for camera framing.
  const box = new Box3();
  const childBox = new Box3();
  vrm.scene.traverse((obj) => {
    if (!obj.isMesh || !obj.geometry) return;
    if (obj.name.startsWith('VRMC_springBone_collider')) return;
    if (!obj.geometry.boundingBox) obj.geometry.computeBoundingBox();
    childBox.copy(obj.geometry.boundingBox).applyMatrix4(obj.matrixWorld);
    box.union(childBox);
  });

  const modelSize = new Vector3();
  const modelCenter = new Vector3();
  box.getSize(modelSize);
  box.getCenter(modelCenter);

  const headNode = vrm.humanoid?.getNormalizedBoneNode('head');
  const eyeHeight = headNode
    ? headNode.getWorldPosition(new Vector3()).y
    : modelCenter.y;

  return { vrm, group, modelSize, modelCenter, eyeHeight };
}
