# AiStudios

Developing film scripts in the modern world — using AI as a creative collaborator.

AiStudios is a lightweight toolkit for writing, structuring, and producing screenplays with AI assistance. Designed to work from any device, including mobile.

---

## What's here

```
aistudios.py        — Main CLI tool (all AI commands live here)
scripts/            — Your screenplay files (.fountain format)
outlines/           — Story outlines and beat sheets
characters/         — Character profiles
prompts/            — System prompts that shape AI behavior (edit these to tune the AI's voice)
requirements.txt    — Python dependencies
```

---

## Setup

```bash
pip install anthropic
export ANTHROPIC_API_KEY=your_key_here
```

That's it. No build step. No server. Just Python.

---

## Commands

### Start a new script

```bash
python aistudios.py new my_film --title "The Long Way Home" --author "Your Name"
```

Creates `scripts/my_film.fountain` with a proper Fountain header.

---

### Generate a scene

```bash
python aistudios.py scene "Two estranged brothers meet at their father's funeral and argue over the will" \
  --characters "DAVID (older, controlled), JAKE (younger, angry)" \
  --tone "tense, restrained" \
  --output my_film.fountain
```

Generates a scene in [Fountain format](https://fountain.io) and optionally appends it to your script.

---

### Build a story outline

```bash
python aistudios.py outline "The Long Way Home" \
  "A burnt-out ER nurse discovers her recently deceased mother had a secret second family across town" \
  --genre "drama" \
  --length "100"
```

Saves a structured outline to `outlines/`.

---

### Develop a character

```bash
python aistudios.py character "A mid-50s ex-cop who became a florist after a shooting incident he won't talk about" \
  --name "Ray Okafor" \
  --role "protagonist" \
  --genre "thriller"
```

Saves a full character profile to `characters/`.

---

### Rewrite a scene

```bash
python aistudios.py rewrite --file scripts/scene_01.fountain \
  "Make the dialogue less on-the-nose. The characters should talk around the real issue."
```

---

### Generate dialogue

```bash
python aistudios.py dialogue \
  "A wife tells her husband she's leaving, but they're at a dinner party and can't make a scene" \
  --characters "CLAIRE (composed, decisive), NOEL (oblivious until he isn't)" \
  --subtext "She's been planning this for months. He thinks everything is fine."
```

---

## Fountain Format

Scripts are saved in [Fountain](https://fountain.io) — an open plain-text format for screenplays. It's readable as-is and can be converted to industry-standard PDFs using:

- [Fade In](https://www.fadeinpro.com/) (Mac/iOS)
- [Highland 2](https://quoteunquoteapps.com/highland-2/) (Mac/iOS)
- [Afterwriting](https://afterwriting.com/) (free, browser-based)
- [Fountain.io](https://fountain.io/apps) — full list of apps

---

## Customizing AI behavior

The `prompts/` directory contains the system prompts that shape how the AI writes. Edit them to change the AI's voice, style, or priorities:

- `prompts/scene.txt` — how scenes are written
- `prompts/outline.txt` — how outlines are structured  
- `prompts/character.txt` — how characters are developed

---

## Working from iPhone

This project is designed to be picked up from any device. Via Claude Code:

- Browse and edit files directly in the session
- Run commands in the terminal
- Continue mid-project across sessions

The only external dependency is your Anthropic API key set as an environment variable.
