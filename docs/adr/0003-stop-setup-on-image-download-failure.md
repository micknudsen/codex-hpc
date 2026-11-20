# Prepare the matching remote release with setup

V1 uses `setup` for both first installation and upgrades. After updating the local client, the user reruns `setup` to prepare the matching helper and image for new sessions. Running sessions keep the release they started with.

After a successful upgrade, `setup` immediately removes obsolete managed helpers and images that are no longer in use. Queued and running sessions retain the files they need. When the last session using an obsolete file exits, its cleanup removes that file. The next `setup` retries cleanup missed after an interruption.

Before submitting a Slurm job, `connect` checks that the matching remote release is ready. If it is missing, `connect` exits with an error and prints the exact `setup` command for the selected host and installation paths.

On every connection, `connect` rechecks the cached image's SHA-256 checksum and verifies its signature against the client release's public key on the login host. If either check fails, `connect` reports the failure and exits before submitting the job.

The remote workspace must already exist. During the same pre-submission checks, `connect` verifies that the supplied path is a directory on the login host. A missing directory or a path to a regular file produces an error.

The workspace argument accepts an absolute POSIX path or a path beginning with `~/`, resolved against the remote login user's home directory. For a bare relative path such as `project`, the error suggests an absolute path or the quoted form `'~/project'`.

Each v1 client release identifies one official signed Linux x86-64 SIF by its original GitHub Release asset URL, SHA-256 checksum of the signed bytes, and trusted public key. Multiple client releases can reference the same asset. GitHub Releases is the sole image source. Keep the original asset available for every published client that references it.

During release preparation, compare the candidate's image build inputs with the provenance for a previously published image. Include the definition file, target architecture, base image digest, pinned Codex and dependency versions, build tools and arguments, and the paths and contents of scripts and files used by the build. Record a deterministic fingerprint of these resolved inputs alongside the signed SIF checksum. A change confined to client or helper code outside the image leaves the input fingerprint unchanged.

Reuse the original asset when the complete inputs match, its checksum and signature verify against trusted release metadata, and the candidate client and helper pass compatibility tests with those exact bytes. Record the source release and digest in the candidate's metadata. Reuse requires no rebuild, signing step, or duplicate upload. For a first image, changed inputs, or incomplete provenance, build, sign, and qualify a new image with recorded inputs. A failed integrity or compatibility check blocks publication until resolved.

The cluster cache identifies images by architecture and signed SIF checksum across client upgrades. Keep a cached image while the active installation or any queued or running session references it.

All remote setup work runs on the login host. If the matching image is absent from the managed cache, `setup` downloads the release asset. On every setup run it checks the image's SHA-256 checksum and verifies its signature with `apptainer verify` and the bundled public key. A cached image that passes both checks is reused without a download. A failed check stops setup before activation. Only `connect` requests a Slurm job and checks the compute environment on the assigned node.

Keeping a single download route removes the need for a transfer-mode option. If the download fails, setup reports the error and exits. Any previously working installation remains usable.
