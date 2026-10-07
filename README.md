# Ratech Order Management System

A web application for processing PDF orders and managing inventory integration with Sage 50 2019.

## Features

### Phase 1 (Current)
- **PDF Order Processing**: Upload PDF orders and extract text using Google Vision API
- **Order Validation**: Review and edit extracted order data before export
- **CSV Export**: Generate CSV files compatible with Sage 50 2019 import
- **Manual Upload**: Start with manual PDF upload (email integration planned for Phase 2)

### Phase 2 (Planned)
- **Email Integration**: Automatically fetch PDF orders from email
- **Inventory Management**: Photo-based inventory counting
- **Discrepancy Alerts**: Automated alerts for inventory mismatches
- **Approval Workflow**: Email approval system for exceptions

## Setup Instructions

### Prerequisites
- Python 3.8 or higher
- Google Cloud account with Vision API enabled
- Streamlit account (for cloud deployment)

### Local Development

1. **Clone or navigate to the project directory**
   ```bash
   cd "Ratech Admin App"
   ```

2. **Create a virtual environment**
   ```bash
   python -m venv venv
   venv\Scripts\activate  # On Windows
   # or
   source venv/bin/activate  # On Mac/Linux
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up Google Cloud Vision API**
   - Go to [Google Cloud Console](https://console.cloud.google.com)
   - Create a new project or select existing one
   - Enable the Cloud Vision API
   - Create a service account and download the JSON credentials
   - Copy the credentials and paste them in the app Settings page
   - Or save the JSON file and update `.env` with the path

5. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env and add your Google Cloud credentials
   ```

6. **Run the application**
   ```bash
   streamlit run app.py
   ```

   The app will open in your browser at `http://localhost:8501`

### Deployment to Streamlit Cloud

1. **Push your code to GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/yourusername/ratech-admin-app.git
   git push -u origin main
   ```

2. **Deploy to Streamlit Cloud**
   - Go to [Streamlit Cloud](https://share.streamlit.io)
   - Click "New app"
   - Connect your GitHub repository
   - In the "Secrets" section, add your Google Cloud credentials as `GOOGLE_CREDENTIALS`
   - Deploy!

## Sage 50 CSV Format

**Important**: Please provide a sample Sage 50 2019 CSV import format. The current implementation uses a placeholder format. Once you provide the actual format, update the CSV export logic in `app.py`.

## Project Structure

```
Ratech Admin App/
├── app.py                 # Main Streamlit application
├── requirements.txt        # Python dependencies
├── .env.example          # Environment variables template
├── .gitignore            # Git ignore rules
├── README.md             # This file
└── temp_uploads/         # Temporary file storage (gitignored)
```

## Development Roadmap

- [x] Basic Streamlit app structure
- [x] PDF upload functionality
- [ ] Google Vision API integration for text extraction
- [ ] Order data parsing logic
- [ ] Sage 50 CSV format implementation
- [ ] Email integration for automatic PDF fetching
- [ ] Inventory photo processing
- [ ] Discrepancy detection and alerts
- [ ] Approval workflow system

## Troubleshooting

### Google Vision API Errors
- Ensure your service account has the Vision API enabled
- Check that your credentials JSON is correctly formatted
- Verify the credentials have proper permissions

### PDF Processing Issues
- Ensure PDFs are not password-protected
- Check that PDFs contain readable text (not just scanned images)
- For scanned PDFs, OCR will be used

### Deployment Issues
- Make sure all secrets are properly configured in Streamlit Cloud
- Check that `requirements.txt` includes all dependencies
- Verify the app runs locally before deploying

## Support

For issues or questions, please contact your development team or check the documentation:
- [Streamlit Documentation](https://docs.streamlit.io)
- [Google Cloud Vision API](https://cloud.google.com/vision/docs)
