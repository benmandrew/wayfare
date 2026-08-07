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
      formatter = forAllSystems (pkgs: pkgs.nixfmt-rfc-style);

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
            pkgs.nixfmt-rfc-style
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
