# Stockholm Weather Time Capsule

Explore Stockholm's historical daily temperatures and curated events. This project uses the daily dataset for arbitrary dates and currently has two sourced history entries.

Run `python app.py` with Python 3.11 or newer from this directory. Open [the local preview](http://127.0.0.1:8767/) while the terminal is running. On Linux the command may be `python3 app.py`. The preview server binds to localhost and is for development. `CAPSULE_PORT` can change the port.

The app defaults to `stockholm_daily_mean_temperature.csv` next to `app.py`. Use `CAPSULE_DAILY_CSV` only when the verified dataset is stored in a different location. Install the locked dependencies as described in the [runbook](DEPLOYMENT.md), then run `python -m unittest discover -s tests -v` with that environment. Tests cover fixture dates, the published dataset and simulated Bedrock responses.

## Data and interpretation

Anders Moberg (2020), *Stockholm Historical Weather Observations — Daily mean air temperatures since 1756*, dataset version 2, Bolin Centre Database, [DOI 10.17043/stockholm-historical-daily-temperature-2](https://doi.org/10.17043/stockholm-historical-daily-temperature-2), Open Data Commons Attribution Licence (ODC-By). The repository's daily CSV is a snapshot. The page reads the actual first and last dates when the app starts.

The main reading, same-date comparison and 1871–1900 reference average all use the adjusted column. The comparison selects the newest **later** date with the same month and day and a usable adjusted reading. For 29 February, only available leap days contribute. The baseline reports how many days were included. Missing readings remain unavailable, and the occasional Uppsala estimate is labelled. A comparison of two individual days does not establish a trend.

History uses the [Stockholm City Museum exhibition film](https://stockholmskallan.stockholm.se/post/28971), the [Nobel Prize archive](https://www.nobelprize.org/ceremony/from-the-first-nobel-prize-award-ceremony-1901/) and the [Royal House biography of Oskar II](https://www.kungahuset.se/english/royal-house/the-bernadotte-dynasty/oskar-ii). Dates outside the curated selection show weather without conjectural history.

## Create a time capsule

Once the [Bedrock billing and identity checks](DEPLOYMENT.md#prove-bedrock-access-locally) pass, launch the app with `python app.py --bedrock --port 8769` from the environment that has the locked SDK and your AWS sign-in. The operator selects the Bedrock model at launch. Nova Lite is the default. Set `CAPSULE_BEDROCK_MODEL=qwen` to use Qwen3 235B A22B 2507 instead. Both models run in Stockholm. Open [the local AI preview](http://127.0.0.1:8769/), choose a date and select **Create time capsule**. The server gathers the adjusted temperature, comparisons and any curated history for the selected date and sends them to the selected model. Compare the dispatch with the weather figures and archive links on the page.

Both previews run on loopback and allow one generation at a time. The default is five attempts per rolling hour per process. The operator can set the Bedrock limit to ten for the protected demo. A failed attempt counts towards the limit. The app keeps up to 64 successful dispatches in memory and shows the saved text when a reader revisits the date. Cached dispatches use no new model calls. Restarting the app clears them. The app displays generated output as plain text and leaves the source facts visible if the provider is unavailable.

For an optional local Mistral test, first confirm Mistral API eligibility and usage allowance. Launch with `python app.py --mistral --port 8769` and enter the key at the hidden prompt if it is not already set in that terminal. The two provider options are mutually exclusive. Keep the key out of chat, files and Git.

## Release documentation

The [beginner’s guide](docs/BEGINNER-GUIDE.md) covers Ubuntu commands, opening and pausing the demo, and the Terraform and Ansible workflow.

The [deployment runbook](DEPLOYMENT.md) covers local AWS verification, Terraform, Ansible and cleanup.

## Licensing

The project source has no open-source licence. The weather dataset is available separately under the Open Data Commons Attribution Licence described above.
