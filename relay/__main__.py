"""Allow running with: python -m relay"""
from dotenv import load_dotenv
load_dotenv()

from .daemon import main
main()
