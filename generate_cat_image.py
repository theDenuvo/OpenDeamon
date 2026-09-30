import requests
import urllib.parse

prompt = "realistic cat on a white windowsill"
encoded_prompt = urllib.parse.quote(prompt)
url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=300&height=400&nologo=true"
response = requests.get(url)
if response.status_code == 200:
    with open("cat_windowsill.png", "wb") as f:
        f.write(response.content)
else:
    raise Exception(f"Failed to fetch image: {response.status_code}")