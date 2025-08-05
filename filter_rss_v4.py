#
# This script filters RSS feeds from multiple scientific journals to select articles matching specific keywords and performs additional validation using the Gemini API.
#
# Key Features:
# 1. Batch processing of multiple journal RSS feeds.
# 2. Initial filtering using WHITELIST and BLACKLIST keywords.
# 3. Secondary filtering for entries not caught in the initial phase using the Gemini API (minimizing API calls with batch processing).
# 4. Automatic fallback to a backup model and retry mechanism in case of Gemini API quota errors.
# 5. Logs the name of the journal where an error occurred in a state file to resume from that point in the next execution.
# 6. Records 'SUCCESS' in the state file if all journals are processed successfully, ensuring the next execution starts from the beginning.
# 7. Generates email body files containing filtered and removed results.
# 8. Creates an index.html page and individual .xml files for filtered RSS feeds.
# 9. **(Added)** Automatically appends the current GitHub Action run link at the bottom of the email.
# 10. **(Added)** Organizes the email body content by journal.
# 11. **(Added)** Adds emojis to the email body based on filtering method (keyword or Gemini).
# 12. **(Added)** Adds buttons for navigating to the filtering results page in index.html.
# 13. **(Added)** Differentiates the removal method (keyword or Gemini) in the email body for removed articles.
# 14. **(Added)** Provides clickable links in an HTML page that replicates the email body format when the 'Filter Results' button is clicked.
# 15. **(Added)** Displays the last update time in index.html in both Texas and Korean time zones.
# 16. **(Added)** Applies separate keyword and Gemini filter rules for arXiv and PRB journals.
#

... (remaining code content) ...

            </div>
        </div>
        <footer class="text-center mt-4 text-gray-500">
            <p>Created by <a href="https://scholar.google.com/citations?user=-xRqXwUAAAAJ&hl=ko" target="_blank" class="text-indigo-600 hover:underline">Jounghoon Hyun</a></p>
        </footer>
    </div>
</body>
</html>
