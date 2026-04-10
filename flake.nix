{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    treefmt-nix.url = "github:numtide/treefmt-nix";
  };

  outputs = { self, nixpkgs, treefmt-nix }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      treefmtEval = eachSystem (pkgs: treefmt-nix.lib.evalModule pkgs ./treefmt.nix);
    in
    {
      formatter = eachSystem (pkgs: treefmtEval.${pkgs.system}.config.build.wrapper);

      checks = eachSystem (pkgs: {
        formatting = treefmtEval.${pkgs.system}.config.build.check self;
      });

      devShells = eachSystem (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            (python3.withPackages (ps: [ ps.ruff ps.mypy ps.pytest ps.vulture ]))
          ];
          shellHook = ''
            echo "ruff check .          lint"
            echo "ruff format .         format"
            echo "ruff format --check . format (dry run)"
            echo "mypy .                type check"
            echo "pytest tests/ -v      run tests"
            echo "vulture .              dead code"
            echo "nix flake check       run all checks"
          '';
        };
      });
    };
}
