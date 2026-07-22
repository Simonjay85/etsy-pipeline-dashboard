from deep_translator import GoogleTranslator
text = """Hello world.

Here is a list:
- Item 1
- Item 2

Thank you."""
print(repr(GoogleTranslator(source='en', target='vi').translate(text)))
