SET NOCOUNT ON

DECLARE @Id INT = ?

SELECT TOP 1
  Id,
  Name,
  Quantity
FROM
  Items
WHERE
  Id = @Id
