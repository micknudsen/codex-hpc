# Build a signed candidate

Run this on a disposable Linux x86-64 build machine, outside cluster setup. A candidate is unpublished: its versioned GitHub asset URL is the intended original location, not a claim that the asset exists. Publishing requires the separate release workflow.

`image/inputs.json` pins the amd64 base manifest, Codex and ripgrep versions and archive checksums, Apptainer package checksum, Python version, squashfs-tools version, uidmap/fakeroot package versions and build arguments. The base digest pins all operating-system packages; their exact versions are also recorded after execution. No package resolver runs inside the build. The definition, included probe and build scripts are fingerprinted by relative path and content. Changes to client/helper source outside the image leave this fingerprint unchanged.

On a fresh Ubuntu 24.04 CI runner, install the pinned tools using `sh scripts/install_apptainer_ci.sh`. The script uses sudo and modifies that disposable runner's packages and fakeroot configuration. Use Python 3.12.3 and install the wheel build dependencies:

```sh
python -m pip install setuptools==80.9.0 wheel==0.45.1
python scripts/build_candidate.py fingerprint
```

Supply a PEM private key and matching PEM public key outside the repository and output directory. For CI only, generate a disposable identity in a temporary directory:

```sh
umask 077
mkdir -p /tmp/codex-hpc-signing
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out /tmp/codex-hpc-signing/private.pem
openssl pkey -in /tmp/codex-hpc-signing/private.pem -pubout -out /tmp/codex-hpc-signing/public.pem
python scripts/build_candidate.py prepare --version 0.1.0.dev0 \
  --output /tmp/codex-hpc-candidate \
  --private-key /tmp/codex-hpc-signing/private.pem \
  --public-key /tmp/codex-hpc-signing/public.pem
python tests/qualify_candidate.py /tmp/codex-hpc-candidate
```

The output directory must not exist. It appears only after build, signing, offline verification, runtime probing, packaging and installed-client compatibility succeed. It contains the signed SIF, provenance, definition, client wheel, matching self-contained helper and public trust material. Its `source/` directory contains the complete build/packaging source layout: enter that directory to repeat the commands above, with a new output path and your own signing identity. It never contains the private key. The wheel embeds `codex_hpc/release/{release.json,helper.py,trusted.pem}`. The helper's `--describe` reports the bundled release and interface identities.

For a later candidate, supply the earlier image/provenance directory and independently trusted release metadata and public key. Obtain the latter from the previously trusted client/release, not from an untrusted image download. The earlier directory contains `provenance.json` and `codex-hpc-linux-amd64.sif`; its bytes are never modified.

```sh
python scripts/build_candidate.py prepare --version 0.2.0 \
  --output /tmp/codex-hpc-next \
  --previous /path/to/earlier-image \
  --trusted-release /path/to/trusted-client/release.json \
  --public-key /path/to/trusted-client/trusted.pem
```

Complete equal input records select reuse. Missing/incomplete provenance or changed inputs select a new build, which additionally requires `--private-key` and its matching `--public-key`. First preparation also selects a new build. An integrity, trust or compatibility failure on the reuse branch stops preparation; it never silently rebuilds. Supplying a private key on the reuse branch does not cause it to be read or used.

Reuse retains the original versioned GitHub URL, signed digest, image identity and source release. Its output has no SIF, preventing an accidental duplicate-image upload. `selection.json` records the decision and build/sign actions; `upload_image` describes the later release publication plan, and `published` remains false. Preparation never uploads assets, signs a reused image, moves tags or changes a previous asset. A new-image output includes its SIF and plans that new asset for the separately approved release workflow. The low-level `build` command remains available for explicitly building an image; use `prepare` for the complete selection and installed-client compatibility gate.

`compatibility.json` records the exact signed digest tested through a clean-installed candidate wheel. Controlled SSH, download and Slurm peers exercise a cold setup fetching the original URL, a warm setup without another download, and connect running the pinned Codex executable and writing in the mounted workspace with real Apptainer. The generated Codex executable and arguments run `login status` with disposable file auth before the deterministic workspace probe, so the probe cannot hide incompatible command arguments. The actual helper and CLI execute, including owned-job and scratch cleanup. This proves compatibility at the public command boundary without requiring site credentials or a production account. For a reused candidate, the explicit replay command is `python tests/check_compatibility.py /tmp/codex-hpc-next /path/to/earlier-image/codex-hpc-linux-amd64.sif` from its exported `source/` directory.

The SHA-256 and size describe the **signed** bytes. Provenance records the independent image-input fingerprint and observed tools/packages. Metadata identifies `/usr/local/bin/codex`, `/usr/local/bin/rg`, `/usr/bin/git`, `/bin/bash`, and `/usr/bin/python3`, plus the runtime capabilities actually exercised. Qualification installs the wheel in a clean environment, executes its helper, verifies its bundled public key against the real SIF, rejects a wrong key and damaged bytes, and executes the pinned Codex binary without signing in. CI also covers actual container isolation and controlled scheduler/login flows. Real-cluster and OAuth qualification remain separate work.

CI retains the qualified candidate for three days as a workflow artifact for downstream setup testing. These disposable signed candidates are not production releases. An ordinary source checkout has no fabricated release metadata or trust key. No secrets, user paths or cluster configuration are included in the image. Builds record inputs but do not promise byte-for-byte reproducibility.

The pinned build dependencies are based on the [Apptainer installation guide](https://apptainer.org/docs/admin/1.5/installation.html); PEM signing and offline `verify --key` follow the [signing guide](https://apptainer.org/docs/user/1.5/signNverify.html). Codex comes from the [official 0.170.0 release](https://github.com/openai/codex/releases/tag/rust-v0.170.0), and ripgrep from its [15.1.0 release](https://github.com/BurntSushi/ripgrep/releases/tag/15.1.0).
