# Prepare the matching remote release with setup

V1 uses `setup` for both first installation and upgrades. After updating the local client, the user reruns `setup` to prepare the matching helper and image for new sessions. Running sessions keep the release they started with.

After a successful upgrade, `setup` immediately removes obsolete managed helpers and images that are no longer in use. Queued and running sessions retain the files they need. When the last session using an obsolete file exits, its cleanup removes that file. The next `setup` retries cleanup missed after an interruption.

Before submitting a Slurm job, `connect` checks that the matching remote release is ready. If it is missing, `connect` exits with an error and prints the exact `setup` command for the selected host and installation paths.

On every connection, `connect` rechecks the cached image's SHA-256 checksum and verifies its signature against the client release's public key on the login host. If either check fails, `connect` reports the failure and exits before submitting the job.

The remote workspace must already exist. During the same pre-submission checks, `connect` verifies that the supplied path is a directory on the login host. A missing directory or a path to a regular file produces an error.

The workspace argument accepts an absolute POSIX path or a path beginning with `~/`, resolved against the remote login user's home directory. For a bare relative path such as `project`, the error suggests an absolute path or the quoted form `'~/project'`.

Each v1 release publishes one signed Linux x86-64 SIF image as a GitHub Release asset. GitHub Releases is the sole image source. Each client release includes the immutable asset URL, SHA-256 checksum, and public signing key for its matching image.

All remote setup work runs on the login host. If the matching image is absent from the managed cache, `setup` downloads the release asset. On every setup run it checks the image's SHA-256 checksum and verifies its signature with `apptainer verify` and the bundled public key. A cached image that passes both checks is reused without a download. A failed check stops setup before activation. Only `connect` requests a Slurm job and checks the compute environment on the assigned node.

Keeping a single download route removes the need for a transfer-mode option. If the download fails, setup reports the error and exits. Any previously working installation remains usable.
