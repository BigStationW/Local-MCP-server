# Local Gutenberg Books

- [Project Gutenberg](https://en.wikipedia.org/wiki/Project_Gutenberg) is a digital library founded in 1971 that archives a collection of over 70,000 free eBooks (primarily classic literature, philosophy, and history).
- This module allows you to download these books, index them locally into a high-speed search database [(Manticore Search)](https://github.com/manticoresoftware/manticoresearch/), and expose them to your LLM so it can retrieve passages directly from these books.

## Prerequisites
- **Windows OS:** The script uses Windows binaries from Manticore Search.

## 1. Downloading Books

- Double-click on **`Local-MCP-server\gutenberg\download_books.bat`**

This script will automatically download the Manticore Search engine, prepare the database, and let you choose which books to download.

## 2. Activating the Manticore Server

- Double-click on **`Local-MCP-server\gutenberg\launch_gutenberg.bat`** 

This opens a console window running Manticore Search on `127.0.0.1:9306`. 

**Leave this console window open** so the database remains accessible.

## 3. Connecting to your LLM

When both console windows (**`Local-MCP-server\gutenberg\launch.ps1`** and **`Local-MCP-server\launch.bat`**) are open and running, your LLM is now able to use specialized tools to interact with the database:

* **`gutenberg_search`** finds specific books and passages by searching with a set of keywords or full sentences. 

* **`read_book_content`** reads larger passages from a specific book.

# Example

<img width="1920" alt="asasassasas" src="https://github.com/user-attachments/assets/bdb6151e-873d-493f-9fc0-c7ccd154c29a" />

