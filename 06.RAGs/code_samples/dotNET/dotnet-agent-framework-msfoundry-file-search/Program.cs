using System.ClientModel;
using Azure.AI.Projects;
using Azure.AI.Projects.Agents;
using Azure.Identity;
using Microsoft.Agents.AI;
using Microsoft.Agents.AI.Foundry;
using OpenAI;
using OpenAI.Files;
using OpenAI.Responses;
using OpenAI.VectorStores;
using DotNetEnv;

Env.Load("../../../../.env");

var endpoint = Environment.GetEnvironmentVariable("FOUNDRY_PROJECT_ENDPOINT") ?? throw new InvalidOperationException("FOUNDRY_PROJECT_ENDPOINT is not set.");
var deploymentName = Environment.GetEnvironmentVariable("FOUNDRY_MODEL") ?? "gpt-5.4";

// Create an AI Project client and get an OpenAI client that works with the foundry service.
AIProjectClient aiProjectClient = new(
    new Uri(endpoint),
    new AzureCliCredential());
OpenAIClient openAIClient = aiProjectClient.GetProjectOpenAIClient((Azure.AI.Projects.OpenAI.ProjectOpenAIClientOptions?)null);

// Upload the file that contains the data to be used for RAG to the Foundry service.
OpenAIFileClient fileClient = openAIClient.GetOpenAIFileClient();
ClientResult<OpenAIFile> uploadResult = await fileClient.UploadFileAsync(
    filePath: "../../files/demo.md",
    purpose: FileUploadPurpose.Assistants);

#pragma warning disable OPENAI001
VectorStoreClient vectorStoreClient = openAIClient.GetVectorStoreClient();
ClientResult<VectorStore> vectorStoreCreate = await vectorStoreClient.CreateVectorStoreAsync(options: new VectorStoreCreationOptions()
{
    Name = "rag-knowledge-base",
    FileIds = { uploadResult.Value.Id }
});
#pragma warning restore OPENAI001
#pragma warning disable OPENAI001
FileSearchTool fileSearchTool = new([vectorStoreCreate.Value.Id]);
#pragma warning restore OPENAI001

ProjectsAgentVersion agentVersion = await aiProjectClient.AgentAdministrationClient.CreateAgentVersionAsync(
    "dotNETRAGAgent",
    new ProjectsAgentVersionCreationOptions(
        new DeclarativeAgentDefinition(model: deploymentName)
        {
            Instructions = @"You are an AI assistant that helps people find information in a set of documents. Use the File Search tool to look up relevant information from the files when needed to answer user questions. If you don't know the answer, just say you don't know. Do not make up answers.",
            Tools = { fileSearchTool }
        }));

#pragma warning disable OPENAI001
FoundryAgent agent = aiProjectClient.AsAIAgent(agentVersion);
#pragma warning restore OPENAI001

#pragma warning disable OPENAI001
AgentSession session = await agent.CreateSessionAsync();

Console.WriteLine(await agent.RunAsync("What's graphrag?", session));

