# What Tweppy needs from you

A running list of the keys, permissions and decisions the AI features depend on.
Nothing here is stored in the repository. Keys go in `.env` locally, or in your
host's environment variables (Koyeb, Render), and Tweppy must be restarted after
you add them.

## 1. Keys and permissions

| # | What | Needed for | Where to get it | Status |
|---|---|---|---|---|
| 1 | `OPENAI_API_KEY` | GPT text and GPT Image | https://platform.openai.com/api-keys | Needed (or #2) |
| 2 | `GEMINI_API_KEY` | Gemini text and Gemini images | https://aistudio.google.com/apikey | Needed (or #1) |
| 3 | X `media.write` permission | Posting images | Run `python authorize.py` again and approve | Needed for image posts |
| 4 | X API credits | Every post and image upload | X developer console | Already needed today |

Notes:

- **One AI key is enough.** Either provider can write the text and make the
  images. With both set, you choose per job in **AI Studio > Models**, for
  example Gemini for text and GPT Image for pictures.
- **OpenAI image models** can require a verified organization on your OpenAI
  account before the API will serve them. If "Check connection" works but
  images fail with a 403, that is the cause.
- **Gemini image generation** is not on the free tier. The key's Google Cloud
  project needs billing turned on.
- **`media.write` (#3):** the token you authorized before this feature existed
  cannot upload images. Text posts keep working without it. After the first
  image post, **AI Studio > Setup checklist** shows whether X accepted it.

## 2. Things to confirm once the keys are in

These could not be checked while building, because no keys were available and
the provider documentation was not reachable from the build environment.
Everything below was tested only against a stand-in server.

- [ ] **Model names.** Model IDs change often, so they are settings and not
      fixed in code. The defaults are `gpt-5-mini` and `gpt-image-1` (OpenAI)
      and `gemini-2.5-flash` and `gemini-3.1-flash-image` (Gemini). Press
      **Check connection** in the Setup checklist: it lists the models your key
      can use, warns if a default is not among them, and fills the suggestions
      in the model fields.
- [ ] **One real text request and one real image** from each provider you plan
      to use (Compose with AI, 1 post, format "Image + caption").
- [ ] **One real image post to X.** The upload uses `POST /2/media/upload` and
      then attaches the result to the post. Use "Post now" on a draft with an
      image.

## 3. Decisions for you

- **Review before posting.** Autopilot is off by default, and when you turn it
  on, its posts wait in Drafts for your approval. To let it post with no
  review, untick "Review autopilot posts before they go out" in AI Studio.
- **Posts per day, format mix and posting window** start at 3 a day, 30% with
  an image, between 08:00 and 21:00 in the timezone set in Settings. Set the
  timezone first; it defaults to UTC.
- **Spend.** Each batch of posts is one text request, and each image is one
  image request. Images cost far more than text. AI Studio shows how many
  requests have been made, but not their price. Set a monthly limit in the
  OpenAI and Google consoles.

## 4. Not built yet

- **Replying and other interaction** (answering mentions, liking, quoting).
  This needs paid X read access to fetch mentions, plus rules for what the bot
  may answer on its own. Tell me if you want it and how hands-off it should be.
- **Timing from your own numbers.** "AI picks the best times" relies on what
  the model knows about your audience and timezone. It does not read your
  account's engagement, because that also needs paid X read access.
- **AI posts into Communities.** Drafts go to the profile queue or schedule.
- **Video, GIFs and several images per post.** One still image per post.
- **Uploading your own images.** Images come from the image model only.
