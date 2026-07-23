"""로컬 stdio 진입점: python3 -m yupack_mcp.local 또는 uvx yupack"""
from .server import local_main


def main():
    local_main()


if __name__ == "__main__":
    main()
