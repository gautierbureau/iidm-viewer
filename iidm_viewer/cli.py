import subprocess
import sys
from pathlib import Path


def _suppress_streamlit_email_prompt():
    credentials = Path.home() / ".streamlit" / "credentials.toml"
    if not credentials.exists():
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_text('[general]\nemail = ""\n')


def main():
    _suppress_streamlit_email_prompt()
    app_path = Path(__file__).parent / "app.py"
    sys.exit(subprocess.call(["streamlit", "run", str(app_path)] + sys.argv[1:]))


if __name__ == "__main__":
    main()
