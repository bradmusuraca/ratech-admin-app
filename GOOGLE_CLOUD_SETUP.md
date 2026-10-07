# Google Cloud Vision API Setup Guide

Follow these steps to set up Google Cloud credentials for the Ratech Order Management System.

## Step 1: Create or Select a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Sign in with your Google account
3. Click the project dropdown at the top
4. Click "NEW PROJECT" or select an existing project
5. Give it a name (e.g., "Ratech Order Management")
6. Click "CREATE"

## Step 2: Enable the Vision API

1. In the Google Cloud Console, make sure your project is selected
2. Click the hamburger menu (☰) in the top left
3. Navigate to **APIs & Services** > **Library**
4. Search for "Vision API"
5. Click on "Cloud Vision API"
6. Click the **ENABLE** button

## Step 3: Create a Service Account

1. In the Google Cloud Console, navigate to **IAM & Admin** > **Service Accounts**
2. Click **+ CREATE SERVICE ACCOUNT** at the top
3. Fill in the details:
   - **Service account name**: ratech-vision-api
   - **Service account description**: Vision API for Ratech Order Management
4. Click **CREATE AND CONTINUE**
5. Skip adding roles for now (we'll add permissions in the next step)
6. Click **DONE**

## Step 4: Grant Permissions to Service Account

1. Click on the service account you just created (ratech-vision-api)
2. Go to the **Permissions** tab
3. Click **+ GRANT ACCESS**
4. In "Grant access to", type "Service Account Token Creator" and select it
5. Click **SAVE**

## Step 5: Create and Download Credentials JSON

1. While still in the service account page, go to the **Keys** tab
2. Click **ADD KEY** > **Create new key**
3. Select **JSON** as the key type
4. Click **CREATE**
5. The JSON file will automatically download to your computer
6. **Important**: Keep this file secure and never commit it to Git!

## Step 6: Add Credentials to the Application

You have two options:

### Option A: Add via Settings Page (Recommended)

1. Run the Streamlit app: `streamlit run app.py`
2. Navigate to the **Settings** page
3. Paste the entire contents of the downloaded JSON file into the "Google Cloud Credentials JSON" text area
4. Click **Save Credentials**

### Option B: Add via .env File

1. Copy the entire contents of the downloaded JSON file
2. Open the `.env` file in the project directory
3. Add this line (paste the JSON as a single line, removing newlines):
   ```
   GOOGLE_CREDENTIALS={"type":"service_account","project_id":"your-project-id",...}
   ```
4. Save the file

## Step 7: Verify the Setup

1. Run the application: `streamlit run app.py`
2. Go to the Settings page - you should see a green checkmark indicating credentials are configured
3. Upload a test PDF to verify text extraction works

## Troubleshooting

### "API not enabled" Error
- Make sure you enabled the Cloud Vision API in Step 2
- It may take a few minutes for the API to become active

### "Permission denied" Error
- Verify the service account has the correct permissions
- Check that the credentials JSON is correctly formatted

### Credentials Not Saving
- Ensure the `.env` file is writable
- Check that you're pasting the entire JSON content

## Security Best Practices

- **Never** commit the credentials JSON file to Git
- **Never** share the credentials file publicly
- Store the JSON file in a secure location
- Consider using environment variables in production (Streamlit Cloud Secrets)
- Regularly rotate your service account keys

## Streamlit Cloud Deployment

When deploying to Streamlit Cloud:

1. Do NOT commit credentials to the repository
2. In Streamlit Cloud, go to your app settings
3. Navigate to **Secrets** (or **Advanced** > **Secrets**)
4. Add a new secret:
   - Name: `GOOGLE_CREDENTIALS`
   - Value: Paste your JSON credentials (as a single line)
5. Redeploy your application

## Cost Considerations

The Cloud Vision API has a free tier:
- First 1,000 units per month are free
- After that, pricing applies (approximately $1.50 per 1,000 units as of 2024)
- Each page of PDF text detection counts as 1 unit
- Monitor your usage in the Google Cloud Console to avoid unexpected charges
