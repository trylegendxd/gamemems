# OLX Flip Bot v2

Changes:
- Bigger smart blacklist for damaged/risky ads
- Braga/sub-region location filter
- No lxml dependency
- run_bot.bat for Windows
- Keeps running forever while the terminal is open

Run:
pip install -r requirements.txt
copy .env.example .env
notepad .env
python bot.py

Or double-click run_bot.bat after setup.

For the location filter to work best:
1. Open OLX in your browser.
2. Search the item.
3. Apply location filters for Braga / nearby area.
4. Copy the final URL into config.yml.
