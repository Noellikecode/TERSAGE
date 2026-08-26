/**
 * Types for the `three-r128` alias.
 *
 * The alias is a real second copy of three, pinned to r128, installed beside
 * the modern one that `3d-tiles-renderer` requires (>= 0.167). DefinitelyTyped
 * publishes no separate package for it, so the modern type surface is reused.
 *
 * That is sound for the subset `StructureModel` uses -- Scene, WebGLRenderer,
 * OrthographicCamera, Shape, ExtrudeGeometry, Mesh, the standard materials and
 * lights -- all of which are unchanged between r128 and today. It is NOT sound
 * for anything the two versions renamed, so the renderer deliberately avoids
 * the renamed surface: no `outputEncoding`/`outputColorSpace`, no
 * `sRGBEncoding`, no `useLegacyLights`. If a future edit needs one of those,
 * it has to be reached through an explicit cast, not by trusting these types.
 */
declare module 'three-r128' {
  export * from 'three';
}
