# Job Application Helper

A personal Playwright workflow for finding open Summer 2027 internships from the SimplifyJobs README and preparing applications with Jobright. It can use an existing Chrome session, apply a tailored Jobright resume, work through supported application forms, and submit them.

This is a personal automation project, not an official SimplifyJobs, Jobright, or employer product. Application sites change often; review every answer and attached resume before using it.

## Setup

1. Install Python 3.10 or newer and Google Chrome.
2. Install the dependency:

   ```powershell
   python -m pip install -r requirements.txt
   ```
3. Copy `candidate.example.json` to `candidate.json` and enter your own details. Keep `candidate.json` private; it is ignored by Git.
4. Make sure Jobright has your base resumes, named so their filenames start with the configured `resume_prefix` and end in `_SkillBank` (for example, `First_Last_Resume_SDE_SkillBank`). Adjust the prefix to your actual Jobright resume names.
5. Start Chrome setup:

   ```powershell
   python apply_bot.py setup
   ```

   Sign in to Google and Jobright, install/enable the Jobright extension, then close that Chrome window. The helper uses a separate Chrome profile at `~/chrome-job-bot-profile`.

## Use

Preview currently open listings:

```powershell
python apply_bot.py list
```

Run a two-job dry run:

```powershell
python apply_bot.py run --limit 2 --dry-run
```

A real run clicks employer Submit when the form is complete:

```powershell
python apply_bot.py run --limit 2
```

The script asks for an account password at runtime. `APPLY_PASSWORD` can be set in the local shell instead; never commit it. `applied.json` tracks attempts, and `debug/` contains troubleshooting captures. Both are ignored by Git.

CAPTCHAs, employer-specific questions, sign-in checks, or missing information may require manual completion. Do not let the automation guess eligibility, work authorization, dates, or other personal facts. Check the final resume and answers on each employer site.

## Configuration files

- `candidate.example.json`: safe template to copy.
- `candidate.json`: local personal configuration; never share or commit it.
- `candidate-confirmed.json`: optional local record of answers already confirmed by the candidate; never share or commit it.

## Limitations

Selectors and supported flows are based on the portals encountered during development. Some portals will require manual work or code changes. The script only recognizes specific job categories and resume naming conventions; update `RESUME_RULES` and the local resume prefix for your own resumes.
