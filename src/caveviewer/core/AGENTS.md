# Core instructions

These rules supplement the repository-level and source-level `AGENTS.md` files
for files under `src/caveviewer/core/`.

## Quick navigation

- [Scope and component boundaries](#scope-and-component-boundaries)
- [Data and cache safety](#data-and-cache-safety)
- [Threading model](#threading-model)
- [OpenGL and rendering ownership](#opengl-and-rendering-ownership)
- [Shared state and synchronization](#shared-state-and-synchronization)
- [Worker lifecycle and shutdown](#worker-lifecycle-and-shutdown)
- [Queues, backpressure, and resource states](#queues-backpressure-and-resource-states)
- [Public APIs and diagnostics](#public-apis-and-diagnostics)
- [Testing expectations](#testing-expectations)
- [Concurrency pre-merge checklist](#concurrency-pre-merge-checklist)

## Scope and component boundaries

- Keep `caveviewer.core` independent of `caveviewer.gui`, Tk, and concrete
  OpenGL APIs. Inject callbacks or small interfaces at the boundary when
  render-thread work is required.
- Prefer pure functions for scheduling, budgeting, parsing policy, and spatial
  calculations. Keep filesystem, environment, and thread orchestration at the
  edges.
- Treat large-map behavior as a primary constraint. Avoid whole-file reads,
  unbounded queues, and temporary arrays proportional to the entire expanded
  mesh unless the design explicitly accounts for their memory cost.
- Keep worker state transitions synchronized. Renderer callbacks are external
  operations; commit internal loaded/unloaded state only at the transaction
  point defined by the callback contract.
- Do not expose partially constructed resources to rendering code.
- Make initialization and destruction idempotent where practical.
- Ensure shutdown remains safe when initialization fails midway.

## Data and cache safety

- Validate all file inputs before trusting sizes, counts, offsets, dimensions,
  or encoded payload lengths.
- Stop reading and raise an explicit exception when a file is malformed,
  truncated, too large, or inconsistent with the expected format.
- Avoid whole-file reads for chunk, mesh, image, or cache data unless the caller
  has already bounded the input size.
- Cache writes must use private staging locations and clean them after every
  failure, including cancellation, `ENOSPC`, and worker exceptions.
- Cache format changes require an explicit version decision, validation tests,
  and a documented compatibility or rebuild path.
- Ensure CPU-side buffers remain valid until OpenGL upload operations have
  completed as required by the API and binding.
- Avoid retaining references to temporary or mutable buffer objects whose
  contents may change during upload.

## Threading model

- Assume Python is the primary programming language and OpenGL is used for
  rendering.
- Do not assume Python's Global Interpreter Lock makes application state
  thread-safe.
- Use worker threads for file loading, mesh parsing, decompression, network
  access, preprocessing, and other non-OpenGL work.
- Use process-based parallelism instead of threads for CPU-bound Python work
  when the workload does not release the Global Interpreter Lock effectively.
- Account for serialization, memory transfer, and process startup costs before
  using multiprocessing.
- Do not pass live OpenGL contexts or GPU object handles between processes.
- Keep GPU interaction in the process that owns the active OpenGL context.
- Use the simplest threading model that satisfies the application's
  requirements.
- Prefer this implementation order:
  - Single-threaded rendering and application logic.
  - Single render thread with background CPU workers.
  - Message passing and immutable frame snapshots.
  - Shared state protected by explicit synchronization.
  - Shared OpenGL contexts.
  - Advanced multicontext or asynchronous GPU resource systems.
- Introduce shared OpenGL contexts or advanced synchronization only after
  profiling demonstrates a concrete need.
- Prioritize correctness and predictable lifecycle management over maximum
  concurrency.

## OpenGL and rendering ownership

- Treat the OpenGL context as thread-affine.
- Create, use, and destroy an OpenGL context on the thread that owns it unless
  the windowing and OpenGL libraries explicitly support context migration.
- Keep rendering operations on a dedicated render thread or the main
  application thread.
- Do not issue OpenGL commands from arbitrary worker threads.
- Do not perform OpenGL resource creation, upload, modification, or deletion in
  worker threads unless those threads own valid shared OpenGL contexts and the
  synchronization model is explicitly documented.
- Transfer prepared CPU-side data from worker threads to the render thread
  before uploading it to the GPU.
- Define clear ownership for meshes, textures, shaders, buffers, framebuffers,
  and other rendering resources.
- Document which thread owns each OpenGL context and each GPU resource
  lifecycle.
- Do not access or destroy OpenGL resources after the owning context has been
  destroyed.
- Ensure the render thread processes resource cleanup requests before
  application shutdown.
- Keep shader compilation and program linking on a context-owning thread.
- If background shader compilation uses shared contexts, document context
  sharing, resource visibility, synchronization, and driver limitations.
- Do not assume OpenGL object creation in one shared context is immediately
  visible or safe to use in another without synchronization.
- Treat OpenGL drivers as implementation-dependent and test multicontext
  behavior on all supported platforms.
- Prefer one OpenGL context and one render thread unless multiple contexts
  provide a measured and necessary benefit.
- Avoid adding multiple rendering threads solely to increase performance.
- Measure before introducing additional contexts, shared-context resource
  loading, or complex GPU synchronization.
- Process window-system events on the thread required by the selected windowing
  library.
- Follow the thread-affinity requirements of GLFW, SDL, Qt, Pygame, GLUT, or
  any other windowing toolkit in use.
- Do not assume different Python OpenGL libraries have identical threading
  guarantees.
- Verify threading behavior against the specific OpenGL binding, context
  library, driver, and platform.
- Do not call blocking OpenGL readback operations on the render thread unless
  the latency impact is acceptable.
- Move CPU-side processing of screenshots, pixel data, and readback results to
  worker threads after the data has been safely transferred from OpenGL.
- Treat persistent mapped OpenGL buffers as shared memory requiring both
  CPU-side and GPU-side synchronization.
- Use the correct OpenGL synchronization primitives when CPU and GPU operations
  depend on one another.
- Do not use Python thread locks as a substitute for GPU synchronization.
- Distinguish CPU thread synchronization from GPU command ordering.
- Use fences, barriers, flushes, and synchronization objects only when required
  by the OpenGL execution model.
- Avoid unnecessary calls that force CPU-GPU synchronization.

## Shared state and synchronization

- Protect all shared mutable Python objects accessed by multiple threads.
- Minimize shared mutable state between the render thread, worker threads, and
  application logic.
- Prefer immutable data, message passing, queues, and ownership transfer over
  direct shared-state mutation.
- Use thread-safe queues for communication between worker threads and the render
  thread.
- Protect compound state transitions with a single synchronization boundary.
- Do not assume that individually thread-safe operations make a larger
  multi-step operation thread-safe.
- Keep lock scopes as small as practical without exposing inconsistent state.
- Do not hold locks while performing slow file operations, network access,
  shader compilation, large data conversions, or user-defined callbacks.
- Do not call unknown or externally supplied callbacks while holding internal
  locks.
- Establish a consistent global lock-acquisition order when multiple locks may
  be held.
- Avoid nested locks whenever possible.
- Avoid global mutable rendering state.
- Prefer a single producer or clearly defined ownership model for mutable
  render-state updates.
- Do not allow multiple threads to mutate camera state, scene graphs, transform
  hierarchies, or rendering configuration without explicit synchronization.
- Treat scene-graph traversal and scene-graph mutation as separate phases where
  practical.
- Do not modify collections while another thread may be iterating over them.
- Use snapshots, double buffering, command queues, or immutable frame-state
  objects to pass render state safely.
- Prefer double-buffered application state when simulation and rendering run on
  separate threads.
- Publish a complete frame state atomically rather than exposing partially
  updated objects.
- Keep frame timing, input processing, simulation updates, and rendering
  ownership clearly separated.
- Avoid concurrent modification of NumPy arrays shared between threads unless
  access is explicitly synchronized.
- Treat NumPy operations as potentially releasing the Global Interpreter Lock.
- Do not assume a NumPy operation is isolated from concurrent mutation by
  another thread.
- Do not pass writable array views across threads without a clear ownership and
  synchronization policy.
- Prefer read-only arrays or ownership transfer for mesh vertices, indices,
  normals, texture data, and instance data.
- Do not share mutable ctypes buffers, memoryviews, or mapped memory regions
  across threads without explicit synchronization.

## Worker lifecycle and shutdown

- Avoid detached threads.
- Every thread must have a clear owner responsible for startup, cancellation,
  shutdown, and joining.
- Use cooperative cancellation for long-running background operations.
- Ensure blocked worker threads can be awakened during shutdown.
- Define whether pending tasks are completed, cancelled, or discarded during
  shutdown.
- Join all non-daemon worker threads before interpreter termination.
- Do not rely on daemon threads for critical cleanup.
- Do not use sleep calls as a synchronization mechanism.
- Use events, conditions, barriers, futures, or queues for coordination.
- Review all blocking operations for deadlock, starvation, reentrancy, and
  shutdown hazards.
- Do not hold the render loop waiting indefinitely for worker-thread results.
- Use placeholders, cached resources, or deferred loading where appropriate.
- Define timeouts and failure behavior for background tasks required by
  rendering.
- Propagate worker-thread exceptions to the owning application component.
- Do not silently discard exceptions raised in worker threads or futures.
- Ensure background failures do not leave partially initialized scene objects or
  leaked OpenGL resources.

## Queues, backpressure, and resource states

- Use bounded queues for render commands, asset-loading requests, decoded
  images, mesh data, and completed tasks.
- Define behavior when queues are full.
- Prevent producers from generating work faster than the render thread or GPU
  can consume it.
- Avoid unbounded accumulation of textures, meshes, upload requests, or deferred
  deletion requests.
- Batch small render-thread commands when practical to reduce synchronization
  and queue overhead.
- Avoid excessive cross-thread communication per frame.
- Prefer coarse frame-level or task-level communication over fine-grained
  per-object synchronization.
- Keep resource state transitions explicit, such as loading, CPU-ready,
  upload-pending, GPU-ready, failed, and deletion-pending.

## Public APIs and diagnostics

- Encapsulate thread ownership and synchronization inside clearly defined
  components.
- Document whether public methods may be called from any thread, only from the
  render thread, or only before startup.
- Reject or queue calls made from the wrong thread rather than executing them
  unpredictably.
- Consider asserting thread identity in render-thread-only components during
  development.
- Do not rely on log ordering to infer actual execution ordering.
- Include thread names, task identifiers, frame numbers, and resource
  identifiers in concurrency-related logs.
- Avoid excessive logging inside locks or the render loop.
- Ensure diagnostic and profiling code does not introduce new races.
- Remember that bugs may originate in Python code, C extensions, OpenGL
  bindings, window-system libraries, graphics drivers, or GPU synchronization.
- Treat crashes inside native libraries as possible symptoms of incorrect object
  lifetime, context ownership, or cross-thread OpenGL access.

## Testing expectations

- Put focused tests in `tests/unit/core/`; add an integration test when thread,
  filesystem, or parser/cache boundaries interact.
- Test startup, normal operation, resizing, context recreation, asset loading,
  error handling, and shutdown under concurrent load.
- Test rapid application close while background loading is active.
- Test resource deletion while uploads or scene transitions are pending.
- Test repeated context creation and destruction where supported.
- Test minimized, hidden, or temporarily unavailable rendering surfaces where
  relevant.
- Run stress tests with many asset requests, queue saturation, repeated scene
  changes, and forced worker failures.
- Use deterministic synchronization hooks in tests rather than timing-dependent
  sleeps.
- Use Python concurrency diagnostics and native thread-safety tools where
  applicable.

## Concurrency pre-merge checklist

Before merging concurrency-related changes, confirm:

- Every OpenGL context has a documented owner thread.
- No arbitrary worker thread issues OpenGL commands.
- All shared mutable Python and NumPy state is synchronized.
- Resource creation and deletion occur on valid context-owning threads.
- Worker exceptions are propagated.
- Queues are bounded.
- Shutdown wakes and joins all workers.
- Pending GPU resources are safely finalized or discarded.
- No callbacks execute under locks without explicit justification.
- The design has been tested under load and during shutdown.
