"""Static defaults: subjects, presets, provider presets and limits."""

from __future__ import annotations

APP_DIR_NAME = "AIExamSimulator"
DATA_DIR_ENV = "AIES_DATA_DIR"

DB_FILENAME = "exam_simulator.db"
CONFIG_FILENAME = "config.json"

# Import / processing limits
MAX_IMPORT_FILE_BYTES = 50 * 1024 * 1024
MAX_PASTED_TEXT_CHARS = 2_000_000
MAX_SOURCE_CHARS_PER_REQUEST = 6_000
SUPPORTED_IMPORT_EXTENSIONS = (".pdf", ".txt", ".docx", ".md", ".markdown")
SUPPORTED_BANK_EXTENSIONS = (".json",)

DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Subjects with suggested topics. Users can always type custom subjects/topics.
SUBJECTS: dict[str, list[str]] = {
    "Mathematics": ["Algebra", "Calculus", "Geometry", "Trigonometry", "Linear Algebra", "Probability"],
    "Physics": ["Mechanics", "Thermodynamics", "Electromagnetism", "Optics", "Waves", "Modern Physics"],
    "Chemistry": ["Stoichiometry", "Atomic Structure", "Chemical Bonding", "Thermochemistry", "Organic Chemistry", "Acids and Bases"],
    "Biology": ["Cell Biology", "Genetics", "Evolution", "Ecology", "Human Physiology", "Microbiology"],
    "Computer Science": ["Data Structures", "Algorithms", "Complexity", "Operating Systems", "Networks", "Databases"],
    "Programming": ["Python Basics", "Control Flow", "Functions", "Object-Oriented Programming", "Debugging", "Recursion"],
    "Statistics": ["Descriptive Statistics", "Probability Distributions", "Hypothesis Testing", "Regression", "Sampling"],
    "Economics": ["Microeconomics", "Macroeconomics", "Supply and Demand", "Market Structures", "Monetary Policy"],
    "History": ["Ancient History", "Medieval History", "Modern History", "World Wars", "Cold War"],
    "Geography": ["Physical Geography", "Human Geography", "Climate", "Cartography", "Natural Resources"],
    "Languages": ["Grammar", "Vocabulary", "Reading Comprehension", "Writing", "Translation"],
    "Business and Finance": ["Accounting", "Corporate Finance", "Marketing", "Management", "Investments"],
    "Law": ["Constitutional Law", "Contract Law", "Criminal Law", "Torts", "International Law"],
}

LANGUAGES = ["English", "Czech", "German", "French", "Spanish", "Italian", "Polish", "Portuguese", "Slovak"]

# Cloud provider presets. All use the OpenAI-compatible Chat Completions protocol.
# Model names are intentionally NOT hard-coded: the user enters one or fetches the
# list from the provider's /models endpoint.
CLOUD_PROVIDER_PRESETS: dict[str, str] = {
    "OpenAI": "https://api.openai.com/v1",
    "Anthropic (OpenAI-compatible)": "https://api.anthropic.com/v1",
    "OpenRouter": "https://openrouter.ai/api/v1",
    "Groq": "https://api.groq.com/openai/v1",
    "Mistral": "https://api.mistral.ai/v1",
    "Custom (OpenAI-compatible)": "",
}

# Ollama models that are reasonable starting points, with rough memory needs.
RECOMMENDED_LOCAL_MODELS: list[tuple[str, str]] = [
    ("llama3.2:3b", "~2 GB download, runs on most PCs (8 GB RAM), basic quality"),
    ("qwen2.5:7b", "~4.7 GB, 16 GB RAM recommended, good structured output"),
    ("llama3.1:8b", "~4.9 GB, 16 GB RAM recommended, good general quality"),
    ("mistral:7b", "~4.1 GB, 16 GB RAM recommended"),
]
