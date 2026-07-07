FROM apify/actor-python-playwright:3.12

COPY requirements.txt ./

RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir -r requirements.txt \
    && echo "All installed:" \
    && pip freeze

COPY . ./

CMD ["python3", "-m", "src"]
