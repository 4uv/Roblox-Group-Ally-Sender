# Roblox Group Ally Request System

A Python application that automatically discovers and sends ally requests to Roblox groups using official Roblox APIs.

## Features

- **Automated Group Discovery**: Scrapes Roblox catalog to find groups from clothing creators
- **Ally Request Management**: Sends ally requests to discovered groups
- **Multi-Account Support**: Rotate between multiple accounts to distribute load
- **Rate Limiting**: Configurable delays and exponential backoff
- **Persistence**: Saves progress between sessions
- **Circuit Breaker**: Prevents cascading failures
- **Discord Notifications**: Real-time updates via webhook
- **Comprehensive Logging**: File and console logging

## Requirements

- Python 3.7+
- `requests` library (install with `pip install -r requirements.txt`)

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Configure `config.json` with your settings (see Configuration section)

3. Run the application:
   ```bash
   python group.py
   ```

## Configuration

Edit `config.json` with your settings:

### Required Fields

- **`cookies`**: Array of Roblox authentication cookies (.ROBLOSECURITY values)
  - **Performance Note**: The more cookies you provide, the faster the script will run as it can distribute requests across multiple accounts, reducing individual account rate limits
- **`webhook`**: Discord webhook URL for notifications
- **`group_id`**: Your group ID that will send ally requests
- **`already_added_file`**: File to track processed groups (e.g., "already_added.txt")

### Optional Fields

- **`queue_file`**: Queue storage file (default: "queue.txt")
- **`min_delay_between_requests`**: Minimum delay between requests in seconds (default: 10)
- **`max_delay_between_requests`**: Maximum delay between requests in seconds (default: 60)
- **`max_retries`**: Maximum retry attempts for failed requests (default: 3)
- **`request_timeout`**: HTTP request timeout in seconds (default: 30)
- **`max_queue_size`**: Maximum queue size limit (default: 10000)
- **`circuit_breaker_threshold`**: Circuit breaker failure threshold (default: 5)
- **`circuit_breaker_timeout`**: Circuit breaker recovery timeout in seconds (default: 300)
- **`log_level`**: Logging verbosity ("DEBUG", "INFO", "WARNING", "ERROR")

### Example config.json

```json
{
    "cookies": [
        "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you-and-to-steal-your-ROBUX-and-items.|_COOKIE_VALUE_HERE"
    ],
    "webhook": "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN",
    "group_id": "12345678",
    "already_added_file": "already_added.txt",
    "queue_file": "queue.txt",
    "min_delay_between_requests": 10,
    "max_delay_between_requests": 60,
    "max_retries": 3,
    "request_timeout": 30,
    "max_queue_size": 10000,
    "circuit_breaker_threshold": 5,
    "circuit_breaker_timeout": 300,
    "log_level": "INFO"
}
```

## How It Works

1. **Asset Scraping**: Discovers clothing items from Roblox catalog
2. **Group Extraction**: Identifies group creators from scraped assets
3. **Queue Management**: Adds new groups to processing queue with deduplication
4. **Ally Requests**: Sends ally requests to queued groups
5. **Ally Discovery**: Finds allies of processed groups to expand network
6. **State Persistence**: Saves progress to prevent data loss on restart

## File Structure

- `group.py`: Main application
- `config.json`: Configuration file
- `requirements.txt`: Python dependencies
- `queue.txt`: Persistent queue of groups to process
- `already_added.txt`: Record of processed groups
- `ally_sender.log`: Application logs
- `*.backup`: Automatic backup files

## Usage

The application runs continuously and will:
- Scrape new groups from the catalog
- Process queued groups by sending ally requests
- Discover new groups from allies
- Save progress automatically
- Handle rate limiting and errors gracefully

Press `Ctrl+C` to stop gracefully.

## Rate Limiting & Performance

The application implements several rate limiting mechanisms:
- Configurable delays between requests
- Exponential backoff on failures
- Multi-account rotation
- Circuit breaker pattern
- Request timeout handling

### Performance Scaling
The script's performance scales with the number of cookies provided:
- **Single account**: Limited by individual rate limits
- **Multiple accounts**: Requests distributed across accounts, significantly faster processing
- **More accounts = More requests per minute**: Base delay is calculated as `max_delay_between_requests ÷ number_of_accounts`
- **Example**: With 60s max delay and 3 accounts, base delay becomes 20s per account cycle
- **Optimal setup**: 3-5 accounts typically provide the best balance of speed and manageability

## Error Handling

- Comprehensive logging to `ally_sender.log`
- Discord webhook notifications for errors
- Automatic retry with exponential backoff
- Graceful handling of invalid cookies
- Circuit breaker prevents cascading failures

## Security Notes

- Never share your Roblox cookies with others
- Use secure webhook URLs
- Monitor the logs for unusual activity
- Consider using dedicated accounts for automation

## Troubleshooting

1. **"No valid cookies found"**: Check that your cookies are valid and not expired
2. **Rate limiting errors**: Increase delay settings in config
3. **Discord webhook failures**: Verify webhook URL is correct
4. **File permission errors**: Check that the application can write to the directory

## Legal & Terms

This tool uses official Roblox APIs and should be used in accordance with Roblox's Terms of Service. Only use with accounts you own and operate responsibly.
