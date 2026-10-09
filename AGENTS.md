# Antigravity Agent Guidelines for Invest Desk

## Standard Model Selection
- **Model podstawowy do konsultacji i architektury**: `claude-opus-5-5-medium`
- **Model zapasowy (fallback)**: `claude-sonnet-5-5-medium`

### Zasady:
1. Przy wszelkich konsultacjach projektowych, recenzjach kodu, weryfikacji założeń merytorycznych, audytach i tworzeniu planów działania korzystamy domyślnie z modelu **Claude Opus 5.5 na poziomie medium** (`claude-opus-5-5-medium`).
2. W razie błędu, przekroczenia limitu zapytań (rate limit) lub niedostępności modelu Opus następuje natychmiastowy fallback na **Claude Sonnet 5.5 na poziomie medium** (`claude-sonnet-5-5-medium`).
3. Narzędzie `tools/consult_model.py` implementuje ten łańcuch wywołań automatycznie zarówno dla CLI jak i importów w Pythonie.
