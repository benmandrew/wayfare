{
  description = "wayfare — UK bus routes snapped to the road network";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # `nix fmt` formats this file.
      formatter = forAllSystems (pkgs: pkgs.nixfmt);

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          # uv still owns the Python dependencies, exactly as the README and
          # CLAUDE.md describe. Nix owns the interpreter and everything that
          # has to be compiled or found on PATH.
          nativeBuildInputs = [
            pkgs.python312
            pkgs.uv
            pkgs.pkg-config
            # The same felt fork, same version, as the Dockerfile builds from
            # source. `publish` shells out to it for native PMTiles output.
            pkgs.tippecanoe
            # Not used by the pipeline; for reading work/wayfare.duckdb by hand.
            pkgs.duckdb
            # ruff formats the Python, nixfmt formats this file. CI checks both.
            pkgs.nixfmt
            # deploy/refresh.sh is the whole of the deployed pipeline, runs
            # unattended under `set -euo pipefail`, and is vendored into Ansible
            # rather than tested by running it. CI checks it.
            pkgs.shellcheck
            # taplo formats and schema-checks the TOML, and is the language server
            # behind the "Even Better TOML" VS Code extension -- so an editor and
            # CI read the one `.taplo.toml` and agree. It is here for
            # `wayfare/map.toml`, whose shape nothing reports at run time: a
            # mistyped layer name draws an empty layer and says nothing.
            pkgs.taplo
            # The two viewer pages carry around 3,000 lines of JavaScript inside
            # `<script>` tags, and ruff and mypy see none of it. Biome reads the
            # script tags, so the pages need no extraction and the repo needs no
            # node_modules. `biome.jsonc` is the configuration.
            pkgs.biome
            # `check.yml` and `image.yml` call each other, and a wrong `needs` or a
            # malformed `if:` expression is a workflow that silently does not run
            # the check it is named for. actionlint is the only thing that reads
            # those files as anything but YAML.
            pkgs.actionlint
            # The Dockerfile builds Valhalla and tippecanoe from source and is
            # otherwise checked only by running it, which takes long enough that a
            # shell slip in a `RUN` is expensive to find. `.hadolint.yaml` is the
            # configuration.
            pkgs.hadolint
          ];

          # buildInputs rather than nativeBuildInputs so the linker wrapper adds
          # an RPATH: pycairo has no wheel, it compiles here and must still find
          # libcairo at import time.
          buildInputs = [ pkgs.cairo ];

          env = {
            # Build against this interpreter rather than letting uv fetch its own
            # standalone Python, which would not see the nix cairo.
            UV_PYTHON = "${pkgs.python312}/bin/python3.12";
            UV_PYTHON_DOWNLOADS = "never";

            # duckdb ships a manylinux wheel that expects a distro libstdc++.
            # Nothing on this interpreter's search path provides one, and the
            # failure is an ImportError at `import duckdb`, not at install.
            LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
              pkgs.stdenv.cc.cc.lib
              pkgs.zlib
            ];
          };

          shellHook = ''
            if [ ! -f pyproject.toml ]; then
              echo "wayfare: no pyproject.toml here; skipping venv setup" >&2
            else
              # Rebuild the venv when the interpreter moves in the store (a
              # nixpkgs bump leaves .venv pointing at a garbage-collected path)
              # and re-sync when the dependency list changes.
              stamp=.venv/.nix-python
              if [ ! -x .venv/bin/python ] \
                || [ "$(cat $stamp 2>/dev/null)" != "${pkgs.python312}" ] \
                || [ pyproject.toml -nt $stamp ]; then
                echo "wayfare: syncing .venv" >&2
                # --python on the install too: UV_PYTHON otherwise wins over
                # venv discovery and uv tries to write into the nix store.
                uv venv --allow-existing .venv >&2 \
                  && uv pip install --python .venv/bin/python --quiet -e '.[dev,art]' >&2 \
                  && printf '%s\n' "${pkgs.python312}" > $stamp
              fi
              export VIRTUAL_ENV="$PWD/.venv"
              export PATH="$PWD/.venv/bin:$PATH"
            fi
          '';
        };
      });
    };
}
