"""Minimal smoke-test app — confirms Streamlit + Databricks Apps infra works."""

import os
import sys

import streamlit as st

st.set_page_config(page_title="App Health Check", page_icon="✅")
st.title("App is running!")
st.write(f"Python: `{sys.version}`")
st.write(f"DATABRICKS_HOST set: `{bool(os.getenv('DATABRICKS_HOST'))}`")
st.write(f"DATABRICKS_TOKEN set: `{bool(os.getenv('DATABRICKS_TOKEN'))}`")
st.write(f"MODEL_ENDPOINT: `{os.getenv('DATABRICKS_MODEL_ENDPOINT', 'not set')}`")
st.success("Streamlit is up and serving on this port.")
