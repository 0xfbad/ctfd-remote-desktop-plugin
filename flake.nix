{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    treefmt-nix.url = "github:numtide/treefmt-nix";
  };

  outputs = {
    self,
    nixpkgs,
    treefmt-nix,
  }: let
    systems = ["x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin"];
    eachSystem = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    treefmtEval = eachSystem (pkgs: treefmt-nix.lib.evalModule pkgs ./treefmt.nix);
  in {
    formatter = eachSystem (pkgs: treefmtEval.${pkgs.system}.config.build.wrapper);

    checks = eachSystem (pkgs: let
      pythonEnv = pkgs.python3.withPackages (ps: [ps.mypy ps.pytest ps.vulture ps.markupsafe ps.jinja2 ps.pyyaml]);
    in {
      formatting = treefmtEval.${pkgs.system}.config.build.check self;
      tests = pkgs.runCommand "ctfd-remote-desktop-tests" {nativeBuildInputs = [pythonEnv pkgs.util-linux];} ''
        cd ${self}
        pytest -p no:cacheprovider tests -q
        touch $out
      '';
      types = pkgs.runCommand "ctfd-remote-desktop-types" {nativeBuildInputs = [pythonEnv];} ''
        cd ${self}
        mypy --cache-dir "$TMPDIR/mypy" .
        touch $out
      '';
      dead-code = pkgs.runCommand "ctfd-remote-desktop-dead-code" {nativeBuildInputs = [pythonEnv];} ''
        cd ${self}
        vulture .
        touch $out
      '';
      shell = pkgs.runCommand "ctfd-remote-desktop-shell" {nativeBuildInputs = [pkgs.shellcheck];} ''
        cd ${self}
        shellcheck setup.sh
        bash -n setup.sh
        touch $out
      '';
    });

    devShells = eachSystem (pkgs: {
      default = pkgs.mkShell {
        packages = with pkgs; [
          uv
          (python3.withPackages (ps: [ps.ruff ps.mypy ps.pytest ps.vulture ps.markupsafe ps.jinja2 ps.pyyaml]))
        ];
        shellHook = ''
          echo "ruff check .          lint"
          echo "ruff format .         format"
          echo "ruff format --check . format (dry run)"
          echo "mypy .                type check"
          echo "pytest tests/ -v      run tests"
          echo "vulture .             dead code"
          echo "nix flake check       run all checks"
        '';
      };
    });
  };
}
