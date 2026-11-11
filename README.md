# Austin 311: How Is My District Served?

A dashboard for Austin City Council district office staff. It answers two questions:

- Is my district being served as quickly as the others?
- Is each kind of request getting fixed, or is a backlog building up?

It uses Austin's public 311 service request data, which the app loads live from the city's open data portal.

## What it shows

**District snapshot** (the first page)

- Requests in the last 30, 60 or 90 days, compared with the period before.
- How many requests are still open, and how long they have been waiting.
- Typical time to resolve, compared with the whole city.
- The top request types, with how many of each are resolved.
- Weekly volume and the most recent requests.

**Request type details** (pick a type from the snapshot)

- **Open now:** the backlog, and how old it is. The card turns red when open requests have waited longer than 9 in 10 closed requests took.
- **District comparison:** how your district compares with the other nine, and a side-by-side with one district you pick.
- **Busy weeks:** weekly volume, with unusually busy weeks marked.
- **Map:** where requests come from, framed to your district.
- **ZIP code table:** ZIP codes sorted so the oldest open backlog is at the top.

## Privacy

The data has no names or contact details. To avoid pointing at individual homes, the app never shows full addresses:

- Locations appear as a street block, such as "5200 block Ledesma Rd", or as a ZIP code.
- The map groups requests into areas of about two city blocks.
- Areas with fewer than 3 requests are hidden.

## Run it on your computer

You need Python 3.10 or newer (tested on 3.13).

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Then open http://localhost:8501.

## Optional: Socrata app token

The app works without any key. Austin limits how often anonymous users can query the data, so a free app token helps if the app gets a lot of use.

- Get a token by signing in at data.austintexas.gov and creating one under Developer Settings.
- **On your computer:** copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and paste the token in. That file is not saved to git.
- **On Streamlit Community Cloud:** paste this line into the app's Secrets settings:

  ```
  SOCRATA_APP_TOKEN = "your-token"
  ```

## Deploy to Streamlit Community Cloud

1. Go to share.streamlit.io and choose **Create app**.
2. Pick this repository, branch `main`, and the file `app.py`.
3. Optionally, add the token in **Advanced settings → Secrets**.

## How the data is handled

- Data is cached for an hour. After that, the app fetches fresh data the next time someone opens a page.
- **Refresh data now** in the sidebar forces an update.
- If the open data portal is down, the app shows the last data it loaded and says so.
- Duplicate, test and transferred requests are left out.
- All times are Austin (Central) time.

## Limits

- **Typical time to resolve** only counts closed requests, so it can look good while a backlog grows. Check **Open now** alongside it.
- The **target resolution** setting (3 days by default) is for demonstration. Austin does not publish an official target for this data.
- Council district numbers come from each request. Austin redrew its districts in 2022, so older requests use the old map.
- ZIP codes and council districts do not line up exactly.

## Files

- `app.py`: the dashboard pages.
- `snapshot.py`: loads the data from Austin and does the calculations.
- `requirements.txt`: the Python packages to install.
- `.streamlit/secrets.toml.example`: a template for the optional token.

## Acknowledgments & Credits

- Built during a school hackathon.
- Prototyped with help from **Kiro**.
- Data provided by the [City of Austin Open Data Portal](https://data.austintexas.gov/).
