# Discord adapter

The output-delivery module exposes `deliver_provider_output` for verified neutral
provider bytes. The caller must validate the admitted tenant, provider session
and authoritative root before supplying a `DeliverableFile`. This edge does
not select outputs or fetch provider content; host/event wiring is separate.

Uploads reuse answer attachment edits and standalone `discord.File` fallback.
Existing answer attachments are preserved. The guild size limit and the current
writer protection still apply.
