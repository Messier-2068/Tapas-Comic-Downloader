# Tapastic-Comic-Downloader
This is a downloader to download available comics from https://tapas.io/. (Not official!)
This fork was reworked with the aid of AI coding. Functionality is tested and verified. 

## Attention:
**This script could be illegal in certain cases, please first read the terms of service on https://tapas.io/ !**

## Usage:
1. Installing python3 and needed modules:
 * On Windows:
   * Install python from python.org
   * Open cmd
   * Navigate to repo location (needs to be downloaded)
   * Install needed modules:
      ```
      pip install -r requirements.txt
      playwright install chromium
      ```
2. Get input link
 * Go to the comic lists
 * Right click comic and copy URL
 * Alternatively: Go into the comic, click on the comic name or thumbnail in the upper right corner, copy url from browser address bar.
 * Examples: `https://tapas.io/series/i-became-the-emperors-cat`, `i-became-the-emperors-cat`, `https://tapas.io/series/295794`, `295794`
3. Start the download
 * Usage of `tapas-dl.py`:
 ```
 usage: tapas-dl.py [SERIES ...] [-l [PATH]] [-f] [-v] [-c [PATH]] [-o [PATH]] [--headed] [-wuf] [-tn]

Scans and downloads comics/novels from 'https://tapas.io'.

positional arguments:
SERIES Tapas series ID, slug, URL, or @file.
Examples:
313219
the-survivor
https://tapas.io/series/313219
https://tapas.io/series/the-survivor
@series.txt

optional arguments:
-l [PATH], --series-file [PATH]
Read series arguments from a text file.
One series per line. Can be supplied multiple times.

-f, --force Reprocess episodes marked complete in the state file.

-v, --verbose Enable verbose output.

-c [PATH], --cookies [PATH]
Optional Netscape/Mozilla cookies.txt file.

-o [PATH], --output-dir [PATH]
Base output directory.
If omitted, series folders are created in the
current working directory.

--headed Show Chromium while loading, scrolling, and
probing the Tapas episode list.

-wuf Allow Playwright to verify and click the first
sequential locked episode

-tn Download thumbnails for ALL discovered episodes.
Default: Thumbnails are downloaded only for
episodes that are being scraped. 
 ```
 * The script will create an folder with the name and urlName (`name [urlName]`) of the comic in the current shell location (like git) and download all images of the comic into it.
 * To specify an base output path use `-o/--output-dir \desired\path` (If not specified, files and folders will be created where the script was run.)
