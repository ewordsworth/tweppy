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
| 3 | X permissions `media.write` and `like.write` | Posting images, liking mentions | Run `python authorize.py` again and approve (once covers both) | Needed for images and likes |
| 4 | X API credits | Every post, reply, image upload, and every read (mentions, post stats) | X developer console | Already needed today; reads are new |
| 5 | X API access that allows reads | Replies tab, engagement learning | X developer console, your app's plan | To confirm |

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
- [ ] **One real mentions check.** Press **Check now** in the Replies tab. It
      calls `GET /2/users/:id/mentions`. If X answers 403, your app's plan does
      not include reads, or the token is missing a permission.
- [ ] **One real reply and one like** from the Replies tab.
- [ ] **One engagement read.** Press **Refresh** under "What works for you" in
      AI Studio. It calls `GET /2/users/:id/tweets` with public metrics.

## 3. Decisions for you

- **Review before posting.** Autopilot is off by default, and when you turn it
  on, its posts wait in Drafts for your approval. To let it post with no
  review, untick "Review autopilot posts before they go out" in AI Studio.
- **Posts per day, format mix and posting window** start at 3 a day, 30% with
  an image, between 08:00 and 21:00 in the timezone set in Settings. Set the
  timezone first; it defaults to UTC.
- **Automatic replies.** Off by default. When on, replies wait for your
  review unless you untick "Review replies before they are sent". The limit
  starts at 20 replies in 24 hours and mentions are checked hourly. Write your
  own rules under "How should it reply?", for example where to send order
  problems.
- **Engagement learning.** Off by default because it is a paid read once a
  day. It needs at least 10 posts before it changes anything.
- **Spend.** Each batch of posts or replies is one text request, and each
  image is one image request. AI Studio shows request counts and a rough
  dollar figure worked out from two per-request costs that you enter under
  Models (they start as placeholders, not real prices). Set a monthly limit in
  the OpenAI and Google consoles as well. X charges separately for posts and
  reads.

## 4. Not built

- **Video, and several images on one post.** One image (still or GIF) per post.
- **Quote posts and reposting other accounts.** Replies and likes only.
- **Replies inside Communities and direct messages.** Mentions only.
- **Following or unfollowing accounts.** Left out on purpose; automated
  follow activity is the quickest way to get an account restricted.
