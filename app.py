import streamlit as st
import os
from dotenv import load_dotenv
import pandas as pd
from io import StringIO
from vision_processor import VisionProcessor, parse_order_text

load_dotenv()

st.set_page_config(
    page_title="Ratech Order Management",
    page_icon="📦",
    layout="wide"
)

st.title("📦 Ratech Order Management System")
st.markdown("Upload PDF orders, validate them, and export to Sage 50 2019 CSV format")

# Sidebar navigation
st.sidebar.title("Navigation")
page = st.sidebar.radio("Select Page", ["Order Processing", "Inventory Management", "Settings"])

if page == "Order Processing":
    st.header("Order Processing")
    
    # Initialize session state for storing processed data
    if 'processed_df' not in st.session_state:
        st.session_state.processed_df = None
    if 'extracted_text' not in st.session_state:
        st.session_state.extracted_text = None
    
    st.subheader("Upload PDF Order")
    uploaded_file = st.file_uploader(
        "Choose a PDF file",
        type=['pdf'],
        help="Upload a PDF order document for processing"
    )
    
    if uploaded_file:
        st.success(f"File uploaded: {uploaded_file.name}")
        
        # Get Google credentials from session state or environment
        google_credentials = os.getenv('GOOGLE_CREDENTIALS')
        
        if not google_credentials:
            st.warning("Google Cloud credentials not configured. Please add them in Settings.")
            st.info("Using placeholder mode for demo purposes")
            extracted_text = "Sample extracted text from PDF\nItem 1: Widget A - $10.00\nItem 2: Widget B - $20.00"
        else:
            with st.spinner("Processing PDF with Google Vision API..."):
                try:
                    processor = VisionProcessor(credentials_json=google_credentials)
                    file_bytes = uploaded_file.getbuffer()
                    extracted_text = processor.extract_text_from_bytes(file_bytes, file_type='pdf')
                    st.success("Text extraction completed!")
                except Exception as e:
                    st.error(f"Error processing PDF: {str(e)}")
                    st.info("Using placeholder mode for demo purposes")
                    extracted_text = "Sample extracted text from PDF\nItem 1: Widget A - $10.00\nItem 2: Widget B - $20.00"
        
        st.session_state.extracted_text = extracted_text
        
        st.subheader("Extracted Order Data")
        st.text_area("Raw Extracted Text", extracted_text, height=200)
        
        # Parse order data
        with st.spinner("Parsing order data..."):
            # Get Gemini API key for LLM-based parsing
            gemini_key = os.getenv('GEMINI_API_KEY')
            
            # Show which parsing method is being used
            if gemini_key:
                st.info("🤖 Using Gemini AI for parsing")
            else:
                st.warning("⚠️ No Gemini API key found - using regex fallback")
            
            order_items = parse_order_text(extracted_text, gemini_api_key=gemini_key)
            
            if order_items:
                df = pd.DataFrame(order_items)
                # Reorder columns for better display
                cols = ['Purchase Order Number', 'Date of Order', 'Ship By Date', 'Ship To', 'Invoice Address', 'Vendor', 'Shipping Instructions', 'Comments', 'Inventory Item', 'Description', 'Quantity', 'Unit Price', 'Total']
                # Only include columns that exist
                cols = [col for col in cols if col in df.columns]
                df = df[cols]
                st.success(f"✅ Successfully parsed {len(order_items)} item(s)")
            else:
                st.warning("⚠️ Could not automatically parse order items.")
                st.info("📝 Please enter data manually using the editable table below.")
                df = pd.DataFrame(columns=["Purchase Order Number", "Date of Order", "Ship By Date", "Ship To", "Invoice Address", "Vendor", "Shipping Instructions", "Comments", "Inventory Item", "Description", "Quantity", "Unit Price", "Total"])
                # Add one empty row for manual entry
                df.loc[0] = [""] * len(df.columns)
        
        st.session_state.processed_df = df
        
        st.subheader("Parsed Order Data")
        st.dataframe(df, use_container_width=True)
    
    # Show processed data if available
    if st.session_state.processed_df is not None:
        st.subheader("Validate and Edit Order")
        
        # Add helpful tips
        with st.expander("💡 Tips for Manual Entry"):
            st.markdown("""
            - **Click any cell** to edit its value
            - **Click "+ Add row"** at the bottom to add new items
            - **Click the "×"** button to delete a row
            - **Common fields** (PO number, dates, addresses) are automatically copied to all rows when parsing
            - **Hover over column headers** to see what information goes there
            """)
        
        edited_df = st.data_editor(
            st.session_state.processed_df,
            num_rows="dynamic",
            use_container_width=True,
            key="order_editor"
        )
        
        # Quick actions for manual entry
        st.subheader("Quick Actions")
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if st.button("Copy Header Info to All Rows"):
                # Copy header fields (PO number, dates, addresses) to all rows
                if not edited_df.empty:
                    header_cols = ['Purchase Order Number', 'Date of Order', 'Ship By Date', 'Ship To', 'Invoice Address', 'Vendor', 'Shipping Instructions', 'Comments']
                    for col in header_cols:
                        if col in edited_df.columns:
                            # Get the first non-empty value
                            first_value = edited_df[col].dropna().iloc[0] if not edited_df[col].dropna().empty else ""
                            if first_value:
                                edited_df[col] = first_value
                    st.session_state.processed_df = edited_df
                    st.success("✅ Header info copied to all rows")
                    st.rerun()
        
        with col2:
            if st.button("Clear All Data"):
                st.session_state.processed_df = None
                st.session_state.extracted_text = None
                st.rerun()
        
        with col3:
            if st.button("Add New Row"):
                if not edited_df.empty:
                    new_row = {col: "" for col in edited_df.columns}
                    # Copy header fields to new row
                    header_cols = ['Purchase Order Number', 'Date of Order', 'Ship By Date', 'Ship To', 'Invoice Address', 'Vendor', 'Shipping Instructions', 'Comments']
                    for col in header_cols:
                        if col in edited_df.columns:
                            first_value = edited_df[col].dropna().iloc[0] if not edited_df[col].dropna().empty else ""
                            if first_value:
                                new_row[col] = first_value
                    edited_df = pd.concat([edited_df, pd.DataFrame([new_row])], ignore_index=True)
                    st.session_state.processed_df = edited_df
                    st.rerun()
        
        # Validation: Check for calculation errors
        if not edited_df.empty:
            errors = []
            price_col = 'Unit Price' if 'Unit Price' in edited_df.columns else ('Price' if 'Price' in edited_df.columns else None)
            
            if price_col and 'Quantity' in edited_df.columns and 'Total' in edited_df.columns:
                for idx, row in edited_df.iterrows():
                    try:
                        qty = float(row['Quantity']) if pd.notna(row['Quantity']) else 0
                        price = float(row[price_col]) if pd.notna(row[price_col]) else 0
                        total = float(row['Total']) if pd.notna(row['Total']) else 0
                        calculated_total = qty * price
                        
                        # Allow small floating point differences
                        if abs(calculated_total - total) > 0.01:
                            errors.append({
                                'Row': idx,
                                'Item': row.get('Description', 'Unknown'),
                                'Quantity': qty,
                                'Price': price,
                                'Total Listed': total,
                                'Total Calculated': calculated_total,
                                'Difference': calculated_total - total
                            })
                    except (ValueError, TypeError):
                        continue
            
            if errors:
                st.error(f"⚠️ Found {len(errors)} calculation error(s):")
                error_df = pd.DataFrame(errors)
                st.dataframe(error_df, use_container_width=True)
                
                st.subheader("Correction Options")
                col1, col2 = st.columns(2)
                
                with col1:
                    if st.button("Auto-Calculate Totals", type="primary"):
                        if price_col and 'Quantity' in edited_df.columns:
                            edited_df['Total'] = edited_df['Quantity'] * edited_df[price_col]
                        st.session_state.processed_df = edited_df
                        st.success("✅ Totals have been recalculated")
                        st.rerun()
                
                with col2:
                    if st.button("Keep Original Totals"):
                        st.info("Original totals will be kept. Please review manually.")
            else:
                st.success("✅ All calculations are correct!")
        
        # Calculate totals (for new entries)
        if not edited_df.empty:
            if 'Unit Price' in edited_df.columns and 'Quantity' in edited_df.columns:
                # Only calculate for rows where Total is empty or zero
                mask = (edited_df['Total'].isna()) | (edited_df['Total'] == 0)
                edited_df.loc[mask, 'Total'] = edited_df.loc[mask, 'Quantity'] * edited_df.loc[mask, 'Unit Price']
            elif 'Price' in edited_df.columns and 'Quantity' in edited_df.columns:
                mask = (edited_df['Total'].isna()) | (edited_df['Total'] == 0)
                edited_df.loc[mask, 'Total'] = edited_df.loc[mask, 'Quantity'] * edited_df.loc[mask, 'Price']
        
        # Update session state with edited data
        st.session_state.processed_df = edited_df
        
        # Export options
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("Export to CSV", type="primary"):
                csv = edited_df.to_csv(index=False)
                st.download_button(
                    label="Download CSV for Sage 50",
                    data=csv,
                    file_name="sage50_order.csv",
                    mime="text/csv"
                )
        
        with col2:
            if st.button("Clear All"):
                st.session_state.processed_df = None
                st.session_state.extracted_text = None
                st.rerun()

elif page == "Inventory Management":
    st.header("Inventory Management")
    st.info("This feature will be implemented in Phase 2. It will allow workers to:")
    st.markdown("""
    - Take photos of packing slips
    - Read inventory counts from images
    - Compare received vs expected quantities
    - Generate alerts for discrepancies
    - Create approval emails for exceptions
    """)

elif page == "Settings":
    st.header("Settings")
    
    st.subheader("Google Cloud Vision API")
    
    # Show current status
    current_creds = os.getenv('GOOGLE_CREDENTIALS')
    if current_creds:
        st.success("✅ Google Cloud credentials are configured")
    else:
        st.warning("⚠️ Google Cloud credentials not configured")
    
    google_credentials = st.text_area(
        "Google Cloud Credentials JSON",
        value=current_creds if current_creds else "",
        height=200,
        help="Paste your Google Cloud service account JSON credentials here"
    )
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("Save Credentials"):
            if google_credentials:
                # Validate JSON format before saving
                try:
                    import json
                    json.loads(google_credentials.strip())
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON format: {e}")
                    st.info("Please ensure you pasted the entire JSON file contents without extra characters.")
                else:
                    # Update .env file
                    env_file = ".env"
                    if os.path.exists(env_file):
                        # Read existing env
                        with open(env_file, 'r') as f:
                            lines = f.readlines()
                        
                        # Update or add GOOGLE_CREDENTIALS
                        updated = False
                        new_lines = []
                        for line in lines:
                            if line.startswith('GOOGLE_CREDENTIALS='):
                                new_lines.append(f'GOOGLE_CREDENTIALS={google_credentials}\n')
                                updated = True
                            else:
                                new_lines.append(line)
                        
                        if not updated:
                            new_lines.append(f'\nGOOGLE_CREDENTIALS={google_credentials}\n')
                        
                        with open(env_file, 'w') as f:
                            f.writelines(new_lines)
                    else:
                        # Create new .env file
                        with open(env_file, 'w') as f:
                            f.write(f'GOOGLE_CREDENTIALS={google_credentials}\n')
                    
                    # Reload environment
                    load_dotenv(override=True)
                    st.success("Credentials saved to .env file")
                    st.rerun()
            else:
                st.warning("Please enter credentials")
    
    with col2:
        if st.button("Clear Credentials"):
            if os.path.exists(".env"):
                with open(".env", 'r') as f:
                    lines = f.readlines()
                
                new_lines = [line for line in lines if not line.startswith('GOOGLE_CREDENTIALS=')]
                
                with open(".env", 'w') as f:
                    f.writelines(new_lines)
                
                load_dotenv(override=True)
                st.success("Credentials cleared")
                st.rerun()
    
    st.divider()
    
    st.subheader("Gemini AI (Optional)")
    st.info("Add a Gemini API key to enable AI-powered parsing for better accuracy with complex formats.")
    
    # Show current status
    current_gemini = os.getenv('GEMINI_API_KEY')
    if current_gemini:
        st.success("✅ Gemini API key is configured")
    else:
        st.info("ℹ️ Gemini API key not configured (optional - will use regex parsing)")
    
    gemini_key = st.text_input(
        "Gemini API Key",
        value=current_gemini if current_gemini else "",
        type="password",
        help="Enter your Gemini API key from Google AI Studio"
    )
    
    if st.button("Save Gemini Key"):
        if gemini_key:
            # Update .env file
            env_file = ".env"
            if os.path.exists(env_file):
                # Read existing env
                with open(env_file, 'r') as f:
                    lines = f.readlines()
                
                # Update or add GEMINI_API_KEY
                updated = False
                new_lines = []
                for line in lines:
                    if line.startswith('GEMINI_API_KEY='):
                        new_lines.append(f'GEMINI_API_KEY={gemini_key}\n')
                        updated = True
                    else:
                        new_lines.append(line)
                
                if not updated:
                    new_lines.append(f'\nGEMINI_API_KEY={gemini_key}\n')
                
                with open(env_file, 'w') as f:
                    f.writelines(new_lines)
            else:
                # Create new .env file
                with open(env_file, 'w') as f:
                    f.write(f'GEMINI_API_KEY={gemini_key}\n')
            
            # Reload environment
            load_dotenv(override=True)
            st.success("Gemini API key saved")
            st.rerun()
        else:
            st.warning("Please enter a Gemini API key")
    
    st.divider()
    
    st.subheader("Sage 50 CSV Format")
    st.info("Please provide a sample Sage 50 2019 CSV import format. This will be used to structure the export.")
    st.markdown("""
    **Expected CSV structure (placeholder):**
    - Item Name
    - Quantity
    - Price
    - Total
    - Date
    - Order Number
    
    Once you provide the actual format, the export logic will be updated accordingly.
    """)
    
    st.divider()
    
    st.subheader("Email Integration")
    st.info("Email integration will be added in a future update to automatically fetch PDF orders from your inbox.")
    st.markdown("""
    **Planned features:**
    - Gmail/Outlook integration
    - Automatic PDF attachment detection
    - Scheduled order processing
    - Email notifications for processed orders
    """)

st.sidebar.markdown("---")
st.sidebar.markdown("### Quick Links")
st.sidebar.markdown("[Google Cloud Console](https://console.cloud.google.com)")
st.sidebar.markdown("[Streamlit Docs](https://docs.streamlit.io)")
