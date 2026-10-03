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
 $ ./tapas-dl.py -h
 usage: tapas-dl.py [URL/name/ID] [-l [PATH]] [-c [PATH]] [-o [PATH]] [-f] [-v] [--headed]
 
 Downloads Comics from 'https://tapas.io'.

 positional arguments:
   URL/name/ID           URL, comic url name, or comic ID
 
 optional arguments:
   -f [PATH], --series-file [PATH]
                        Optional file containing multiple Series URLs, names, or IDs separated by new lines
   -c [PATH], --cookies [PATH]
                         Optional cookies.txt file to load, can be used to allow the script to "log in" and circumvent age verification.
   -o [PATH], --output-dir [PATH]
                         Output directory where comics should be placed.
                         If left blank, the script folder will be used. 
 ```
 * The script will create an folder with the name and urlName (`name [urlName]`) of the comic in the current shell location (like git) and download all images of the comic into it.
 * To specify an base output path use `-o/--output-dir \desired\path` (If not specified, files and folders will be created where the script was run.)
