{
  description = "plane-proj — governed multi-agent delivery through Plane";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # uv owns dependency resolution; uv2nix turns the resulting uv.lock into a
    # nix package set, so `plane-proj` installs fleet-wide from the same lock
    # the devshell uses. No hand-written derivation per dependency.
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, pyproject-nix, uv2nix, pyproject-build-systems }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      # Prefer wheels: plane-sdk and its dependencies all publish them, and a
      # wheel needs no build backend resolved at eval time.
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      pythonSetFor = pkgs:
        (pkgs.callPackage pyproject-nix.build.packages {
          python = pkgs.python312;
        }).overrideScope (nixpkgs.lib.composeManyExtensions [
          pyproject-build-systems.overlays.default
          overlay
        ]);
    in
    {
      packages = forAll (pkgs:
        let pythonSet = pythonSetFor pkgs; in
        rec {
          plane-proj = pythonSet.mkVirtualEnv "plane-proj-env" workspace.deps.default;
          default = plane-proj;
        });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [ python312 uv git ruff rumdl ];
          shellHook = ''
            _root="$PWD"
            while [ "$_root" != "/" ] && [ ! -e "$_root/flake.nix" ]; do
              _root="$(dirname "$_root")"
            done

            # uv resolves the dependencies; nix supplies the interpreter. A
            # downloaded generic-linux python cannot start on NixOS, so uv is
            # forbidden from fetching one.
            export UV_PYTHON="${pkgs.python312}/bin/python3.12"
            export UV_PYTHON_DOWNLOADS=never
            export UV_PROJECT_ENVIRONMENT="$_root/.venv"

            if [ -t 1 ]; then echo "syncing python dependencies with uv…"; fi
            ( cd "$_root" && uv sync --all-groups --quiet ) || \
              echo "uv sync failed — run 'uv sync' in $_root to see why"

            export VIRTUAL_ENV="$_root/.venv"
            export PATH="$_root/.venv/bin:$PATH"
            unset _root

            if [ -t 1 ]; then
              echo "plane-proj — $(python3 --version 2>&1 | cut -d' ' -f2) via uv"
              echo "  tests : pytest"
              echo "  lint  : ruff check ."
            fi
          '';
        };
      });
    };
}
