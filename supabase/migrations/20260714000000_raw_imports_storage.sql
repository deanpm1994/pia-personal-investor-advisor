insert into storage.buckets (id, name, public)
values ('raw-imports', 'raw-imports', false)
on conflict (id) do update set public = false;

create policy "raw imports select own"
on storage.objects for select to authenticated
using (
  bucket_id = 'raw-imports'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

create policy "raw imports insert own"
on storage.objects for insert to authenticated
with check (
  bucket_id = 'raw-imports'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

create policy "raw imports delete own"
on storage.objects for delete to authenticated
using (
  bucket_id = 'raw-imports'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);
