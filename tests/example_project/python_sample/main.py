"""Main entry point for the Python sample project."""
from utils import parse, Renderer


def process_input(text: str) -> str:
    """Parse input text and render the result."""
    data = parse(text)
    renderer = Renderer(width=60)
    output = renderer.render(data)
    return output


def load_data(filepath: str) -> str:
    """Load text data from a file."""
    with open(filepath, "r", encoding="utf-8") as fh:
        return fh.read()


def main() -> None:
    """Application entry point."""
    sample = "hello world this is a test"
    result = process_input(sample)
    print(result)


if __name__ == "__main__":
    main()
